# --------------------------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for license information.
# --------------------------------------------------------------------------------------------
# pylint: disable=line-too-long, consider-using-f-string, logging-format-interpolation, inconsistent-return-statements, broad-except, bare-except, too-many-statements, too-many-locals, too-many-boolean-expressions, too-many-branches, too-many-nested-blocks, pointless-statement, expression-not-assigned, unbalanced-tuple-unpacking, unsupported-assignment-operation

import threading
import sys
import time
from urllib.parse import urlparse
import requests

from azure.cli.core.azclierror import (
    RequiredArgumentMissingError,
    ValidationError,
    ResourceNotFoundError,
    CLIError,
    CLIInternalError,
    InvalidArgumentValueError,
    ArgumentUsageError,
    MutuallyExclusiveArgumentError)
from azure.cli.core.commands.client_factory import get_subscription_id
from azure.cli.core.util import open_page_in_browser
from azure.cli.command_modules.appservice.utils import _normalize_location
from knack.log import get_logger
from knack.prompting import prompt_y_n, prompt as prompt_str

from msrestazure.tools import parse_resource_id, is_valid_resource_id
from msrest.exceptions import DeserializationError

from ._client_factory import handle_raw_exception, handle_non_404_exception
from ._clients import ManagedEnvironmentClient, ContainerAppClient, GitHubActionClient, DaprComponentClient, StorageClient, AuthClient
from ._github_oauth import get_github_access_token
from ._models import (
    ManagedEnvironment as ManagedEnvironmentModel,
    VnetConfiguration as VnetConfigurationModel,
    AppLogsConfiguration as AppLogsConfigurationModel,
    LogAnalyticsConfiguration as LogAnalyticsConfigurationModel,
    Ingress as IngressModel,
    Configuration as ConfigurationModel,
    Template as TemplateModel,
    RegistryCredentials as RegistryCredentialsModel,
    ContainerApp as ContainerAppModel,
    Dapr as DaprModel,
    ContainerResources as ContainerResourcesModel,
    Scale as ScaleModel,
    Container as ContainerModel,
    GitHubActionConfiguration,
    RegistryInfo as RegistryInfoModel,
    AzureCredentials as AzureCredentialsModel,
    SourceControl as SourceControlModel,
    ManagedServiceIdentity as ManagedServiceIdentityModel,
    ContainerAppCertificateEnvelope as ContainerAppCertificateEnvelopeModel,
    ContainerAppCustomDomain as ContainerAppCustomDomainModel,
    AzureFileProperties as AzureFilePropertiesModel,
    CustomDomainConfiguration as CustomDomainConfigurationModel,
    ScaleRule as ScaleRuleModel)

from ._utils import (_validate_subscription_registered, _ensure_location_allowed,
                     parse_secret_flags, store_as_secret_and_return_secret_ref, parse_env_var_flags,
                     _generate_log_analytics_if_not_provided, _get_existing_secrets, _convert_object_from_snake_to_camel_case,
                     _object_to_dict, _add_or_update_secrets, _remove_additional_attributes, _remove_readonly_attributes,
                     _add_or_update_env_vars, _add_or_update_tags, _update_revision_weights, _append_label_weights,
                     _get_app_from_revision, raise_missing_token_suggestion, _infer_acr_credentials, _remove_registry_secret, _remove_secret,
                     _ensure_identity_resource_id, _remove_dapr_readonly_attributes, _remove_env_vars, _validate_traffic_sum,
                     _update_revision_env_secretrefs, _get_acr_cred, safe_get, await_github_action, repo_url_to_name,
                     validate_container_app_name, _update_weights, get_vnet_location, register_provider_if_needed,
                     generate_randomized_cert_name, _get_name, load_cert_file, check_cert_name_availability,
                     validate_hostname, patch_new_custom_domain, get_custom_domains, _validate_revision_name, set_managed_identity,
                     create_acrpull_role_assignment, is_registry_msi_system, clean_null_values, _populate_secret_values,
                     validate_environment_location, safe_set, parse_metadata_flags, parse_auth_flags, _azure_monitor_quickstart,
                     set_ip_restrictions, certificate_location_matches, certificate_matches, generate_randomized_managed_cert_name,
                     check_managed_cert_name_availability, prepare_managed_certificate_envelop)
from ._validators import validate_create, validate_revision_suffix
from ._ssh_utils import (SSH_DEFAULT_ENCODING, WebSocketConnection, read_ssh, get_stdin_writer, SSH_CTRL_C_MSG,
                         SSH_BACKUP_ENCODING)
from ._constants import (MAXIMUM_SECRET_LENGTH, MICROSOFT_SECRET_SETTING_NAME, FACEBOOK_SECRET_SETTING_NAME, GITHUB_SECRET_SETTING_NAME,
                         GOOGLE_SECRET_SETTING_NAME, TWITTER_SECRET_SETTING_NAME, APPLE_SECRET_SETTING_NAME, CONTAINER_APPS_RP,
                         NAME_INVALID, NAME_ALREADY_EXISTS, ACR_IMAGE_SUFFIX, HELLO_WORLD_IMAGE, LOG_TYPE_SYSTEM, LOG_TYPE_CONSOLE,
                         MANAGED_CERTIFICATE_RT, PRIVATE_CERTIFICATE_RT, PENDING_STATUS, SUCCEEDED_STATUS)

logger = get_logger(__name__)


# These properties should be under the "properties" attribute. Move the properties under "properties" attribute
def process_loaded_yaml(yaml_containerapp):
    if not yaml_containerapp.get('properties'):
        yaml_containerapp['properties'] = {}

    if yaml_containerapp.get('identity') and yaml_containerapp['identity'].get('userAssignedIdentities'):
        for identity in yaml_containerapp['identity']['userAssignedIdentities']:
            # properties (principalId and clientId) are readonly and create (PUT) will throw error if they are provided
            # Update (PATCH) ignores them so it's okay to remove them as well
            yaml_containerapp['identity']['userAssignedIdentities'][identity] = {}

    nested_properties = ["provisioningState", "managedEnvironmentId", "environmentId", "latestRevisionName", "latestRevisionFqdn",
                         "customDomainVerificationId", "configuration", "template", "outboundIPAddresses"]
    for nested_property in nested_properties:
        tmp = yaml_containerapp.get(nested_property)
        if tmp:
            yaml_containerapp['properties'][nested_property] = tmp
            del yaml_containerapp[nested_property]

    if "managedEnvironmentId" in yaml_containerapp['properties']:
        yaml_containerapp['properties']["environmentId"] = yaml_containerapp['properties']['managedEnvironmentId']
        del yaml_containerapp['properties']['managedEnvironmentId']

    return yaml_containerapp


def load_yaml_file(file_name):
    import yaml
    import errno

    try:
        with open(file_name) as stream:  # pylint: disable=unspecified-encoding
            return yaml.safe_load(stream.read().replace('\x00', ''))
    except (IOError, OSError) as ex:
        if getattr(ex, 'errno', 0) == errno.ENOENT:
            raise ValidationError('{} does not exist'.format(file_name)) from ex
        raise
    except (yaml.parser.ParserError, UnicodeDecodeError) as ex:
        raise ValidationError('Error parsing {} ({})'.format(file_name, str(ex))) from ex


def create_deserializer():
    from ._sdk_models import ContainerApp  # pylint: disable=unused-import
    from msrest import Deserializer
    import inspect

    sdkClasses = inspect.getmembers(sys.modules["azext_containerapp._sdk_models"])
    deserializer = {}

    for sdkClass in sdkClasses:
        deserializer[sdkClass[0]] = sdkClass[1]

    return Deserializer(deserializer)


def update_containerapp_yaml(cmd, name, resource_group_name, file_name, from_revision=None, no_wait=False):
    yaml_containerapp = process_loaded_yaml(load_yaml_file(file_name))
    if type(yaml_containerapp) != dict:  # pylint: disable=unidiomatic-typecheck
        raise ValidationError('Invalid YAML provided. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.')

    if not yaml_containerapp.get('name'):
        yaml_containerapp['name'] = name
    elif yaml_containerapp.get('name').lower() != name.lower():
        logger.warning('The app name provided in the --yaml file "{}" does not match the one provided in the --name flag "{}". The one provided in the --yaml file will be used.'.format(
            yaml_containerapp.get('name'), name))
    name = yaml_containerapp.get('name')

    if not yaml_containerapp.get('type'):
        yaml_containerapp['type'] = 'Microsoft.App/containerApps'
    elif yaml_containerapp.get('type').lower() != "microsoft.app/containerapps":
        raise ValidationError('Containerapp type must be \"Microsoft.App/ContainerApps\"')

    containerapp_def = None

    # Check if containerapp exists
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except Exception:
        pass

    if not containerapp_def:
        raise ValidationError("The containerapp '{}' does not exist".format(name))

    containerapp_def = None

    # Deserialize the yaml into a ContainerApp object. Need this since we're not using SDK
    try:
        deserializer = create_deserializer()
        containerapp_def = deserializer('ContainerApp', yaml_containerapp)
    except DeserializationError as ex:
        raise ValidationError('Invalid YAML provided. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.') from ex

    # Remove tags before converting from snake case to camel case, then re-add tags. We don't want to change the case of the tags. Need this since we're not using SDK
    tags = None
    if yaml_containerapp.get('tags'):
        tags = yaml_containerapp.get('tags')
        del yaml_containerapp['tags']

    containerapp_def = _convert_object_from_snake_to_camel_case(_object_to_dict(containerapp_def))
    containerapp_def['tags'] = tags

    # After deserializing, some properties may need to be moved under the "properties" attribute. Need this since we're not using SDK
    containerapp_def = process_loaded_yaml(containerapp_def)

    # Change which revision we update from
    if from_revision:
        r = ContainerAppClient.show_revision(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, name=from_revision)
        _update_revision_env_secretrefs(r["properties"]["template"]["containers"], name)
        containerapp_def["properties"]["template"] = r["properties"]["template"]

    # Remove "additionalProperties" and read-only attributes that are introduced in the deserialization. Need this since we're not using SDK
    _remove_additional_attributes(containerapp_def)
    _remove_readonly_attributes(containerapp_def)

    secret_values = list_secrets(cmd=cmd, name=name, resource_group_name=resource_group_name, show_values=True)
    _populate_secret_values(containerapp_def, secret_values)

    # Clean null values since this is an update
    containerapp_def = clean_null_values(containerapp_def)

    # Fix bug with revisionSuffix when containers are added
    if not safe_get(containerapp_def, "properties", "template", "revisionSuffix"):
        if "properties" not in containerapp_def:
            containerapp_def["properties"] = {}
        if "template" not in containerapp_def["properties"]:
            containerapp_def["properties"]["template"] = {}
        containerapp_def["properties"]["template"]["revisionSuffix"] = None

    try:
        r = ContainerAppClient.update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)

        if "properties" in r and "provisioningState" in r["properties"] and r["properties"]["provisioningState"].lower() == "waiting" and not no_wait:
            logger.warning('Containerapp creation in progress. Please monitor the creation using `az containerapp show -n {} -g {}`'.format(
                name, resource_group_name
            ))

        return r
    except Exception as e:
        handle_raw_exception(e)


def create_containerapp_yaml(cmd, name, resource_group_name, file_name, no_wait=False):
    yaml_containerapp = process_loaded_yaml(load_yaml_file(file_name))
    if type(yaml_containerapp) != dict:  # pylint: disable=unidiomatic-typecheck
        raise ValidationError('Invalid YAML provided. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.')

    if not yaml_containerapp.get('name'):
        yaml_containerapp['name'] = name
    elif yaml_containerapp.get('name').lower() != name.lower():
        logger.warning('The app name provided in the --yaml file "{}" does not match the one provided in the --name flag "{}". The one provided in the --yaml file will be used.'.format(
            yaml_containerapp.get('name'), name))
    name = yaml_containerapp.get('name')

    if not yaml_containerapp.get('type'):
        yaml_containerapp['type'] = 'Microsoft.App/containerApps'
    elif yaml_containerapp.get('type').lower() != "microsoft.app/containerapps":
        raise ValidationError('Containerapp type must be \"Microsoft.App/ContainerApps\"')

    # Deserialize the yaml into a ContainerApp object. Need this since we're not using SDK
    containerapp_def = None
    try:
        deserializer = create_deserializer()

        containerapp_def = deserializer('ContainerApp', yaml_containerapp)
    except DeserializationError as ex:
        raise ValidationError('Invalid YAML provided. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.') from ex

    # Remove tags before converting from snake case to camel case, then re-add tags. We don't want to change the case of the tags. Need this since we're not using SDK
    tags = None
    if yaml_containerapp.get('tags'):
        tags = yaml_containerapp.get('tags')
        del yaml_containerapp['tags']

    containerapp_def = _convert_object_from_snake_to_camel_case(_object_to_dict(containerapp_def))
    containerapp_def['tags'] = tags

    # After deserializing, some properties may need to be moved under the "properties" attribute. Need this since we're not using SDK
    containerapp_def = process_loaded_yaml(containerapp_def)

    # Remove "additionalProperties" and read-only attributes that are introduced in the deserialization. Need this since we're not using SDK
    _remove_additional_attributes(containerapp_def)
    _remove_readonly_attributes(containerapp_def)

    # Validate managed environment
    if not containerapp_def["properties"].get('environmentId'):
        raise RequiredArgumentMissingError('environmentId is required. This can be retrieved using the `az containerapp env show -g MyResourceGroup -n MyContainerappEnvironment --query id` command. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.')

    env_id = containerapp_def["properties"]['environmentId']
    env_name = None
    env_rg = None
    env_info = None

    if is_valid_resource_id(env_id):
        parsed_managed_env = parse_resource_id(env_id)
        env_name = parsed_managed_env['name']
        env_rg = parsed_managed_env['resource_group']
    else:
        raise ValidationError('Invalid environmentId specified. Environment not found')

    try:
        env_info = ManagedEnvironmentClient.show(cmd=cmd, resource_group_name=env_rg, name=env_name)
    except:
        pass

    if not env_info:
        raise ValidationError("The environment '{}' in resource group '{}' was not found".format(env_name, env_rg))

    # Validate location
    if not containerapp_def.get('location'):
        containerapp_def['location'] = env_info['location']

    try:
        r = ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)

        if r["properties"] and r["properties"]["provisioningState"] and r["properties"]["provisioningState"].lower() == "waiting" and not no_wait:
            logger.warning('Containerapp creation in progress. Please monitor the creation using `az containerapp show -n {} -g {}`'.format(
                name, resource_group_name
            ))

        if r["properties"] and r["properties"]["configuration"] and r["properties"]["configuration"]["ingress"] and r["properties"]["configuration"]["ingress"]["fqdn"]:
            logger.warning("\nContainer app created. Access your app at https://{}/\n".format(r["properties"]["configuration"]["ingress"]["fqdn"]))
        else:
            logger.warning("\nContainer app created. To access it over HTTPS, enable ingress: az containerapp ingress enable --help\n")

        return r
    except Exception as e:
        handle_raw_exception(e)


def create_containerapp(cmd,
                        name,
                        resource_group_name,
                        yaml=None,
                        image=None,
                        container_name=None,
                        managed_env=None,
                        min_replicas=None,
                        max_replicas=None,
                        scale_rule_name=None,
                        scale_rule_type=None,
                        scale_rule_http_concurrency=None,
                        scale_rule_metadata=None,
                        scale_rule_auth=None,
                        target_port=None,
                        exposed_port=None,
                        transport="auto",
                        ingress=None,
                        revisions_mode="single",
                        secrets=None,
                        env_vars=None,
                        cpu=None,
                        memory=None,
                        registry_server=None,
                        registry_user=None,
                        registry_pass=None,
                        dapr_enabled=False,
                        dapr_app_port=None,
                        dapr_app_id=None,
                        dapr_app_protocol=None,
                        dapr_http_read_buffer_size=None,
                        dapr_http_max_request_size=None,
                        dapr_log_level=None,
                        dapr_enable_api_logging=False,
                        revision_suffix=None,
                        startup_command=None,
                        args=None,
                        tags=None,
                        no_wait=False,
                        system_assigned=False,
                        disable_warnings=False,
                        user_assigned=None,
                        registry_identity=None):
    register_provider_if_needed(cmd, CONTAINER_APPS_RP)
    validate_container_app_name(name)
    validate_create(registry_identity, registry_pass, registry_user, registry_server, no_wait)
    validate_revision_suffix(revision_suffix)

    if registry_identity and not is_registry_msi_system(registry_identity):
        logger.info("Creating an acrpull role assignment for the registry identity")
        create_acrpull_role_assignment(cmd, registry_server, registry_identity, skip_error=True)

    if yaml:
        if image or managed_env or min_replicas or max_replicas or target_port or ingress or\
            revisions_mode or secrets or env_vars or cpu or memory or registry_server or\
            registry_user or registry_pass or dapr_enabled or dapr_app_port or dapr_app_id or\
                startup_command or args or tags:
            not disable_warnings and logger.warning('Additional flags were passed along with --yaml. These flags will be ignored, and the configuration defined in the yaml will be used instead')
        return create_containerapp_yaml(cmd=cmd, name=name, resource_group_name=resource_group_name, file_name=yaml, no_wait=no_wait)

    if not image:
        image = HELLO_WORLD_IMAGE

    if managed_env is None:
        raise RequiredArgumentMissingError('Usage error: --environment is required if not using --yaml')

    # Validate managed environment
    parsed_managed_env = parse_resource_id(managed_env)
    managed_env_name = parsed_managed_env['name']
    managed_env_rg = parsed_managed_env['resource_group']
    managed_env_info = None

    try:
        managed_env_info = ManagedEnvironmentClient.show(cmd=cmd, resource_group_name=managed_env_rg, name=managed_env_name)
    except:
        pass

    if not managed_env_info:
        raise ValidationError("The environment '{}' does not exist. Specify a valid environment".format(managed_env))

    location = managed_env_info["location"]
    _ensure_location_allowed(cmd, location, CONTAINER_APPS_RP, "containerApps")

    external_ingress = None
    if ingress is not None:
        if ingress.lower() == "internal":
            external_ingress = False
        elif ingress.lower() == "external":
            external_ingress = True

    ingress_def = None
    if target_port is not None and ingress is not None:
        ingress_def = IngressModel
        ingress_def["external"] = external_ingress
        ingress_def["targetPort"] = target_port
        ingress_def["transport"] = transport
        ingress_def["exposedPort"] = exposed_port if transport == "tcp" else None

    secrets_def = None
    if secrets is not None:
        secrets_def = parse_secret_flags(secrets)

    registries_def = None
    if registry_server is not None and not is_registry_msi_system(registry_identity):
        registries_def = RegistryCredentialsModel
        registries_def["server"] = registry_server

        # Infer credentials if not supplied and its azurecr
        if (registry_user is None or registry_pass is None) and registry_identity is None:
            registry_user, registry_pass = _infer_acr_credentials(cmd, registry_server, disable_warnings)

        if not registry_identity:
            registries_def["username"] = registry_user

            if secrets_def is None:
                secrets_def = []
            registries_def["passwordSecretRef"] = store_as_secret_and_return_secret_ref(secrets_def, registry_user, registry_server, registry_pass, disable_warnings=disable_warnings)
        else:
            registries_def["identity"] = registry_identity

    dapr_def = None
    if dapr_enabled:
        dapr_def = DaprModel
        dapr_def["enabled"] = True
        dapr_def["appId"] = dapr_app_id
        dapr_def["appPort"] = dapr_app_port
        dapr_def["appProtocol"] = dapr_app_protocol
        dapr_def["httpReadBufferSize"] = dapr_http_read_buffer_size
        dapr_def["httpMaxRequestSize"] = dapr_http_max_request_size
        dapr_def["logLevel"] = dapr_log_level
        dapr_def["enableApiLogging"] = dapr_enable_api_logging

    config_def = ConfigurationModel
    config_def["secrets"] = secrets_def
    config_def["activeRevisionsMode"] = revisions_mode
    config_def["ingress"] = ingress_def
    config_def["registries"] = [registries_def] if registries_def is not None else None
    config_def["dapr"] = dapr_def

    # Identity actions
    identity_def = ManagedServiceIdentityModel
    identity_def["type"] = "None"

    assign_system_identity = system_assigned
    if user_assigned:
        assign_user_identities = [x.lower() for x in user_assigned]
    else:
        assign_user_identities = []

    if assign_system_identity and assign_user_identities:
        identity_def["type"] = "SystemAssigned, UserAssigned"
    elif assign_system_identity:
        identity_def["type"] = "SystemAssigned"
    elif assign_user_identities:
        identity_def["type"] = "UserAssigned"

    if assign_user_identities:
        identity_def["userAssignedIdentities"] = {}
        subscription_id = get_subscription_id(cmd.cli_ctx)

        for r in assign_user_identities:
            r = _ensure_identity_resource_id(subscription_id, resource_group_name, r)
            identity_def["userAssignedIdentities"][r] = {}  # pylint: disable=unsupported-assignment-operation

    scale_def = None
    if min_replicas is not None or max_replicas is not None:
        scale_def = ScaleModel
        scale_def["minReplicas"] = min_replicas
        scale_def["maxReplicas"] = max_replicas

    if scale_rule_name:
        if not scale_rule_type:
            scale_rule_type = "http"
        scale_rule_type = scale_rule_type.lower()
        scale_rule_def = ScaleRuleModel
        curr_metadata = {}
        if scale_rule_http_concurrency:
            if scale_rule_type in ('http', 'tcp'):
                curr_metadata["concurrentRequests"] = str(scale_rule_http_concurrency)
        metadata_def = parse_metadata_flags(scale_rule_metadata, curr_metadata)
        auth_def = parse_auth_flags(scale_rule_auth)
        if scale_rule_type == "http":
            scale_rule_def["name"] = scale_rule_name
            scale_rule_def["custom"] = None
            scale_rule_def["http"] = {}
            scale_rule_def["http"]["metadata"] = metadata_def
            scale_rule_def["http"]["auth"] = auth_def
        else:
            scale_rule_def["name"] = scale_rule_name
            scale_rule_def["http"] = None
            scale_rule_def["custom"] = {}
            scale_rule_def["custom"]["type"] = scale_rule_type
            scale_rule_def["custom"]["metadata"] = metadata_def
            scale_rule_def["custom"]["auth"] = auth_def
        if not scale_def:
            scale_def = ScaleModel
        scale_def["rules"] = [scale_rule_def]

    resources_def = None
    if cpu is not None or memory is not None:
        resources_def = ContainerResourcesModel
        resources_def["cpu"] = cpu
        resources_def["memory"] = memory

    container_def = ContainerModel
    container_def["name"] = container_name if container_name else name
    container_def["image"] = image if not is_registry_msi_system(registry_identity) else HELLO_WORLD_IMAGE
    if env_vars is not None:
        container_def["env"] = parse_env_var_flags(env_vars)
    if startup_command is not None:
        container_def["command"] = startup_command
    if args is not None:
        container_def["args"] = args
    if resources_def is not None:
        container_def["resources"] = resources_def

    template_def = TemplateModel
    template_def["containers"] = [container_def]
    template_def["scale"] = scale_def

    if revision_suffix is not None:
        template_def["revisionSuffix"] = revision_suffix

    containerapp_def = ContainerAppModel
    containerapp_def["location"] = location
    containerapp_def["identity"] = identity_def
    containerapp_def["properties"]["environmentId"] = managed_env
    containerapp_def["properties"]["configuration"] = config_def
    containerapp_def["properties"]["template"] = template_def
    containerapp_def["tags"] = tags

    if registry_identity:
        if is_registry_msi_system(registry_identity):
            set_managed_identity(cmd, resource_group_name, containerapp_def, system_assigned=True)
        else:
            set_managed_identity(cmd, resource_group_name, containerapp_def, user_assigned=[registry_identity])
    try:
        r = ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)

        if is_registry_msi_system(registry_identity):
            while r["properties"]["provisioningState"] == "InProgress":
                r = ContainerAppClient.show(cmd, resource_group_name, name)
                time.sleep(10)
            logger.info("Creating an acrpull role assignment for the system identity")
            system_sp = r["identity"]["principalId"]
            create_acrpull_role_assignment(cmd, registry_server, registry_identity=None, service_principal=system_sp)
            container_def["image"] = image

            registries_def = RegistryCredentialsModel
            registries_def["server"] = registry_server
            registries_def["identity"] = registry_identity
            config_def["registries"] = [registries_def]

            r = ContainerAppClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)

        if "properties" in r and "provisioningState" in r["properties"] and r["properties"]["provisioningState"].lower() == "waiting" and not no_wait:
            not disable_warnings and logger.warning('Containerapp creation in progress. Please monitor the creation using `az containerapp show -n {} -g {}`'.format(name, resource_group_name))

        if "configuration" in r["properties"] and "ingress" in r["properties"]["configuration"] and r["properties"]["configuration"]["ingress"] and "fqdn" in r["properties"]["configuration"]["ingress"]:
            not disable_warnings and logger.warning("\nContainer app created. Access your app at https://{}/\n".format(r["properties"]["configuration"]["ingress"]["fqdn"]))
        else:
            target_port = target_port or "<port>"
            not disable_warnings and logger.warning("\nContainer app created. To access it over HTTPS, enable ingress: "
                                                    "az containerapp ingress enable -n %s -g %s --type external --target-port %s"
                                                    " --transport auto\n", name, resource_group_name, target_port)

        return r
    except Exception as e:
        handle_raw_exception(e)


def update_containerapp_logic(cmd,
                              name,
                              resource_group_name,
                              yaml=None,
                              image=None,
                              container_name=None,
                              min_replicas=None,
                              max_replicas=None,
                              scale_rule_name=None,
                              scale_rule_type="http",
                              scale_rule_http_concurrency=None,
                              scale_rule_metadata=None,
                              scale_rule_auth=None,
                              set_env_vars=None,
                              remove_env_vars=None,
                              replace_env_vars=None,
                              remove_all_env_vars=False,
                              cpu=None,
                              memory=None,
                              revision_suffix=None,
                              startup_command=None,
                              args=None,
                              tags=None,
                              no_wait=False,
                              from_revision=None,
                              ingress=None,
                              target_port=None,
                              registry_server=None,
                              registry_user=None,
                              registry_pass=None):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)
    validate_revision_suffix(revision_suffix)

    # Validate that max_replicas is set to 0-30
    if max_replicas is not None:
        if max_replicas < 1 or max_replicas > 30:
            raise ArgumentUsageError('--max-replicas must be in the range [1,30]')

    if yaml:
        if image or min_replicas or max_replicas or\
           set_env_vars or remove_env_vars or replace_env_vars or remove_all_env_vars or cpu or memory or\
           startup_command or args or tags:
            logger.warning('Additional flags were passed along with --yaml. These flags will be ignored, and the configuration defined in the yaml will be used instead')
        return update_containerapp_yaml(cmd=cmd, name=name, resource_group_name=resource_group_name, file_name=yaml, no_wait=no_wait, from_revision=from_revision)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    new_containerapp = {}
    new_containerapp["properties"] = {}
    if from_revision:
        try:
            r = ContainerAppClient.show_revision(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, name=from_revision)
        except CLIError as e:
            # Error handle the case where revision not found?
            handle_raw_exception(e)

        _update_revision_env_secretrefs(r["properties"]["template"]["containers"], name)
        new_containerapp["properties"]["template"] = r["properties"]["template"]

    # Doing this while API has bug. If env var is an empty string, API doesn't return "value" even though the "value" should be an empty string
    if "properties" in containerapp_def and "template" in containerapp_def["properties"] and "containers" in containerapp_def["properties"]["template"]:
        for container in containerapp_def["properties"]["template"]["containers"]:
            if "env" in container:
                for e in container["env"]:
                    if "value" not in e:
                        e["value"] = ""

    update_map = {}
    update_map['scale'] = min_replicas or max_replicas or scale_rule_name
    update_map['container'] = image or container_name or set_env_vars is not None or remove_env_vars is not None or replace_env_vars is not None or remove_all_env_vars or cpu or memory or startup_command is not None or args is not None
    update_map['ingress'] = ingress or target_port
    update_map['registry'] = registry_server or registry_user or registry_pass

    if tags:
        _add_or_update_tags(new_containerapp, tags)

    if revision_suffix is not None:
        new_containerapp["properties"]["template"] = {} if "template" not in new_containerapp["properties"] else new_containerapp["properties"]["template"]
        new_containerapp["properties"]["template"]["revisionSuffix"] = revision_suffix

    # Containers
    if update_map["container"]:
        new_containerapp["properties"]["template"] = {} if "template" not in new_containerapp["properties"] else new_containerapp["properties"]["template"]
        new_containerapp["properties"]["template"]["containers"] = containerapp_def["properties"]["template"]["containers"]
        if not container_name:
            if len(new_containerapp["properties"]["template"]["containers"]) == 1:
                container_name = new_containerapp["properties"]["template"]["containers"][0]["name"]
            else:
                raise ValidationError("Usage error: --container-name is required when adding or updating a container")

        # Check if updating existing container
        updating_existing_container = False
        for c in new_containerapp["properties"]["template"]["containers"]:
            if c["name"].lower() == container_name.lower():
                updating_existing_container = True

                if image is not None:
                    c["image"] = image

                if set_env_vars is not None:
                    if "env" not in c or not c["env"]:
                        c["env"] = []
                    # env vars
                    _add_or_update_env_vars(c["env"], parse_env_var_flags(set_env_vars))

                if replace_env_vars is not None:
                    # Remove other existing env_vars, then add them
                    c["env"] = []
                    _add_or_update_env_vars(c["env"], parse_env_var_flags(replace_env_vars))

                if remove_env_vars is not None:
                    if "env" not in c or not c["env"]:
                        c["env"] = []
                    # env vars
                    _remove_env_vars(c["env"], remove_env_vars)

                if remove_all_env_vars:
                    c["env"] = []

                if startup_command is not None:
                    if isinstance(startup_command, list) and not startup_command:
                        c["command"] = None
                    else:
                        c["command"] = startup_command
                if args is not None:
                    if isinstance(args, list) and not args:
                        c["args"] = None
                    else:
                        c["args"] = args
                if cpu is not None or memory is not None:
                    if "resources" in c and c["resources"]:
                        if cpu is not None:
                            c["resources"]["cpu"] = cpu
                        if memory is not None:
                            c["resources"]["memory"] = memory
                    else:
                        c["resources"] = {
                            "cpu": cpu,
                            "memory": memory
                        }

        # If not updating existing container, add as new container
        if not updating_existing_container:
            if image is None:
                raise ValidationError("Usage error: --image is required when adding a new container")

            resources_def = None
            if cpu is not None or memory is not None:
                resources_def = ContainerResourcesModel
                resources_def["cpu"] = cpu
                resources_def["memory"] = memory

            container_def = ContainerModel
            container_def["name"] = container_name
            container_def["image"] = image
            container_def["env"] = []

            if set_env_vars is not None:
                # env vars
                _add_or_update_env_vars(container_def["env"], parse_env_var_flags(set_env_vars))

            if replace_env_vars is not None:
                # env vars
                _add_or_update_env_vars(container_def["env"], parse_env_var_flags(replace_env_vars))

            if remove_env_vars is not None:
                # env vars
                _remove_env_vars(container_def["env"], remove_env_vars)

            if remove_all_env_vars:
                container_def["env"] = []

            if startup_command is not None:
                if isinstance(startup_command, list) and not startup_command:
                    container_def["command"] = None
                else:
                    container_def["command"] = startup_command
            if args is not None:
                if isinstance(args, list) and not args:
                    container_def["args"] = None
                else:
                    container_def["args"] = args
            if resources_def is not None:
                container_def["resources"] = resources_def

            new_containerapp["properties"]["template"]["containers"].append(container_def)

    # Scale
    if update_map["scale"]:
        new_containerapp["properties"]["template"] = {} if "template" not in new_containerapp["properties"] else new_containerapp["properties"]["template"]
        if "scale" not in new_containerapp["properties"]["template"]:
            new_containerapp["properties"]["template"]["scale"] = {}
        if min_replicas is not None:
            new_containerapp["properties"]["template"]["scale"]["minReplicas"] = min_replicas
        if max_replicas is not None:
            new_containerapp["properties"]["template"]["scale"]["maxReplicas"] = max_replicas

    scale_def = None
    if min_replicas is not None or max_replicas is not None:
        scale_def = ScaleModel
        scale_def["minReplicas"] = min_replicas
        scale_def["maxReplicas"] = max_replicas
    # so we don't overwrite rules
    if safe_get(new_containerapp, "properties", "template", "scale", "rules"):
        new_containerapp["properties"]["template"]["scale"].pop(["rules"])
    if scale_rule_name:
        if not scale_rule_type:
            scale_rule_type = "http"
        scale_rule_type = scale_rule_type.lower()
        scale_rule_def = ScaleRuleModel
        curr_metadata = {}
        if scale_rule_http_concurrency:
            if scale_rule_type in ('http', 'tcp'):
                curr_metadata["concurrentRequests"] = str(scale_rule_http_concurrency)
        metadata_def = parse_metadata_flags(scale_rule_metadata, curr_metadata)
        auth_def = parse_auth_flags(scale_rule_auth)
        if scale_rule_type == "http":
            scale_rule_def["name"] = scale_rule_name
            scale_rule_def["custom"] = None
            scale_rule_def["http"] = {}
            scale_rule_def["http"]["metadata"] = metadata_def
            scale_rule_def["http"]["auth"] = auth_def
        else:
            scale_rule_def["name"] = scale_rule_name
            scale_rule_def["http"] = None
            scale_rule_def["custom"] = {}
            scale_rule_def["custom"]["type"] = scale_rule_type
            scale_rule_def["custom"]["metadata"] = metadata_def
            scale_rule_def["custom"]["auth"] = auth_def
        if not scale_def:
            scale_def = ScaleModel
        scale_def["rules"] = [scale_rule_def]
        new_containerapp["properties"]["template"]["scale"]["rules"] = scale_def["rules"]
    # Ingress
    if update_map["ingress"]:
        new_containerapp["properties"]["configuration"] = {} if "configuration" not in new_containerapp["properties"] else new_containerapp["properties"]["configuration"]
        if target_port is not None or ingress is not None:
            new_containerapp["properties"]["configuration"]["ingress"] = {}
            if ingress:
                new_containerapp["properties"]["configuration"]["ingress"]["external"] = ingress.lower() == "external"
            if target_port:
                new_containerapp["properties"]["configuration"]["ingress"]["targetPort"] = target_port

    # Registry
    if update_map["registry"]:
        new_containerapp["properties"]["configuration"] = {} if "configuration" not in new_containerapp["properties"] else new_containerapp["properties"]["configuration"]
        if "registries" in containerapp_def["properties"]["configuration"]:
            new_containerapp["properties"]["configuration"]["registries"] = containerapp_def["properties"]["configuration"]["registries"]
        if "registries" not in containerapp_def["properties"]["configuration"] or containerapp_def["properties"]["configuration"]["registries"] is None:
            new_containerapp["properties"]["configuration"]["registries"] = []

        registries_def = new_containerapp["properties"]["configuration"]["registries"]

        _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)
        if "secrets" in containerapp_def["properties"]["configuration"] and containerapp_def["properties"]["configuration"]["secrets"]:
            new_containerapp["properties"]["configuration"]["secrets"] = containerapp_def["properties"]["configuration"]["secrets"]
        else:
            new_containerapp["properties"]["configuration"]["secrets"] = []

        if registry_server:
            if not registry_pass or not registry_user:
                if ACR_IMAGE_SUFFIX not in registry_server:
                    raise RequiredArgumentMissingError('Registry url is required if using Azure Container Registry, otherwise Registry username and password are required if using Dockerhub')
                logger.warning('No credential was provided to access Azure Container Registry. Trying to look up...')
                parsed = urlparse(registry_server)
                registry_name = (parsed.netloc if parsed.scheme else parsed.path).split('.')[0]
                registry_user, registry_pass, _ = _get_acr_cred(cmd.cli_ctx, registry_name)
            # Check if updating existing registry
            updating_existing_registry = False
            for r in registries_def:
                if r['server'].lower() == registry_server.lower():
                    updating_existing_registry = True
                    if registry_user:
                        r["username"] = registry_user
                    if registry_pass:
                        r["passwordSecretRef"] = store_as_secret_and_return_secret_ref(
                            new_containerapp["properties"]["configuration"]["secrets"],
                            r["username"],
                            r["server"],
                            registry_pass,
                            update_existing_secret=True,
                            disable_warnings=True)

            # If not updating existing registry, add as new registry
            if not updating_existing_registry:
                registry = RegistryCredentialsModel
                registry["server"] = registry_server
                registry["username"] = registry_user
                registry["passwordSecretRef"] = store_as_secret_and_return_secret_ref(
                    new_containerapp["properties"]["configuration"]["secrets"],
                    registry_user,
                    registry_server,
                    registry_pass,
                    update_existing_secret=True,
                    disable_warnings=True)

                registries_def.append(registry)

    if not revision_suffix:
        new_containerapp["properties"]["template"] = {} if "template" not in new_containerapp["properties"] else new_containerapp["properties"]["template"]
        new_containerapp["properties"]["template"]["revisionSuffix"] = None

    try:
        r = ContainerAppClient.update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=new_containerapp, no_wait=no_wait)

        if "properties" in r and "provisioningState" in r["properties"] and r["properties"]["provisioningState"].lower() == "waiting" and not no_wait:
            logger.warning('Containerapp update in progress. Please monitor the update using `az containerapp show -n {} -g {}`'.format(name, resource_group_name))

        return r
    except Exception as e:
        handle_raw_exception(e)


def update_containerapp(cmd,
                        name,
                        resource_group_name,
                        yaml=None,
                        image=None,
                        container_name=None,
                        min_replicas=None,
                        max_replicas=None,
                        scale_rule_name=None,
                        scale_rule_type=None,
                        scale_rule_http_concurrency=None,
                        scale_rule_metadata=None,
                        scale_rule_auth=None,
                        set_env_vars=None,
                        remove_env_vars=None,
                        replace_env_vars=None,
                        remove_all_env_vars=False,
                        cpu=None,
                        memory=None,
                        revision_suffix=None,
                        startup_command=None,
                        args=None,
                        tags=None,
                        no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    return update_containerapp_logic(cmd=cmd,
                                     name=name,
                                     resource_group_name=resource_group_name,
                                     yaml=yaml,
                                     image=image,
                                     container_name=container_name,
                                     min_replicas=min_replicas,
                                     max_replicas=max_replicas,
                                     scale_rule_name=scale_rule_name,
                                     scale_rule_type=scale_rule_type,
                                     scale_rule_http_concurrency=scale_rule_http_concurrency,
                                     scale_rule_metadata=scale_rule_metadata,
                                     scale_rule_auth=scale_rule_auth,
                                     set_env_vars=set_env_vars,
                                     remove_env_vars=remove_env_vars,
                                     replace_env_vars=replace_env_vars,
                                     remove_all_env_vars=remove_all_env_vars,
                                     cpu=cpu,
                                     memory=memory,
                                     revision_suffix=revision_suffix,
                                     startup_command=startup_command,
                                     args=args,
                                     tags=tags,
                                     no_wait=no_wait)


def show_containerapp(cmd, name, resource_group_name, show_secrets=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        r = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
        if show_secrets:
            _get_existing_secrets(cmd, resource_group_name, name, r)
        return r
    except CLIError as e:
        handle_raw_exception(e)


def list_containerapp(cmd, resource_group_name=None, managed_env=None):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        containerapps = []
        if resource_group_name is None:
            containerapps = ContainerAppClient.list_by_subscription(cmd=cmd)
        else:
            containerapps = ContainerAppClient.list_by_resource_group(cmd=cmd, resource_group_name=resource_group_name)

        if managed_env:
            env_name = parse_resource_id(managed_env)["name"].lower()
            if "resource_group" in parse_resource_id(managed_env):
                ManagedEnvironmentClient.show(cmd, parse_resource_id(managed_env)["resource_group"], parse_resource_id(managed_env)["name"])
                containerapps = [c for c in containerapps if c["properties"]["environmentId"].lower() == managed_env.lower()]
            else:
                containerapps = [c for c in containerapps if parse_resource_id(c["properties"]["environmentId"])["name"].lower() == env_name]

        return containerapps
    except CLIError as e:
        handle_raw_exception(e)


def delete_containerapp(cmd, name, resource_group_name, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        return ContainerAppClient.delete(cmd=cmd, name=name, resource_group_name=resource_group_name, no_wait=no_wait)
    except CLIError as e:
        handle_raw_exception(e)


def create_managed_environment(cmd,
                               name,
                               resource_group_name,
                               logs_destination="log-analytics",
                               storage_account=None,
                               logs_customer_id=None,
                               logs_key=None,
                               location=None,
                               instrumentation_key=None,
                               infrastructure_subnet_resource_id=None,
                               docker_bridge_cidr=None,
                               platform_reserved_cidr=None,
                               platform_reserved_dns_ip=None,
                               internal_only=False,
                               tags=None,
                               disable_warnings=False,
                               zone_redundant=False,
                               hostname=None,
                               certificate_file=None,
                               certificate_password=None,
                               no_wait=False):
    if zone_redundant:
        if not infrastructure_subnet_resource_id:
            raise RequiredArgumentMissingError("Cannot use --zone-redundant/-z without "
                                               "--infrastructure-subnet-resource-id/-s")
        if not is_valid_resource_id(infrastructure_subnet_resource_id):
            raise ValidationError("--infrastructure-subnet-resource-id must be a valid resource id")
        vnet_location = get_vnet_location(cmd, infrastructure_subnet_resource_id)
        if location:
            if _normalize_location(cmd, location) != vnet_location:
                raise ValidationError(f"Location '{location}' does not match the subnet's location: '{vnet_location}'. "
                                      "Please change either --location/-l or --infrastructure-subnet-resource-id/-s")
        else:
            location = vnet_location

    location = validate_environment_location(cmd, location)

    register_provider_if_needed(cmd, CONTAINER_APPS_RP)
    _ensure_location_allowed(cmd, location, CONTAINER_APPS_RP, "managedEnvironments")

    if (logs_customer_id is None or logs_key is None) and logs_destination == "log-analytics":
        logs_customer_id, logs_key = _generate_log_analytics_if_not_provided(cmd, logs_customer_id, logs_key, location, resource_group_name)

    if logs_destination == "log-analytics":
        log_analytics_config_def = LogAnalyticsConfigurationModel
        log_analytics_config_def["customerId"] = logs_customer_id
        log_analytics_config_def["sharedKey"] = logs_key
    else:
        log_analytics_config_def = None

    app_logs_config_def = AppLogsConfigurationModel
    app_logs_config_def["destination"] = logs_destination if logs_destination != "none" else None
    app_logs_config_def["logAnalyticsConfiguration"] = log_analytics_config_def

    managed_env_def = ManagedEnvironmentModel
    managed_env_def["location"] = location
    managed_env_def["properties"]["appLogsConfiguration"] = app_logs_config_def
    managed_env_def["tags"] = tags
    managed_env_def["properties"]["zoneRedundant"] = zone_redundant

    if hostname:
        customDomain = CustomDomainConfigurationModel
        blob, _ = load_cert_file(certificate_file, certificate_password)
        customDomain["dnsSuffix"] = hostname
        customDomain["certificatePassword"] = certificate_password
        customDomain["certificateValue"] = blob
        managed_env_def["properties"]["customDomainConfiguration"] = customDomain

    if instrumentation_key is not None:
        managed_env_def["properties"]["daprAIInstrumentationKey"] = instrumentation_key

    if infrastructure_subnet_resource_id or docker_bridge_cidr or platform_reserved_cidr or platform_reserved_dns_ip:
        vnet_config_def = VnetConfigurationModel

        if infrastructure_subnet_resource_id is not None:
            vnet_config_def["infrastructureSubnetId"] = infrastructure_subnet_resource_id

        if docker_bridge_cidr is not None:
            vnet_config_def["dockerBridgeCidr"] = docker_bridge_cidr

        if platform_reserved_cidr is not None:
            vnet_config_def["platformReservedCidr"] = platform_reserved_cidr

        if platform_reserved_dns_ip is not None:
            vnet_config_def["platformReservedDnsIP"] = platform_reserved_dns_ip

        managed_env_def["properties"]["vnetConfiguration"] = vnet_config_def

    if internal_only:
        if not infrastructure_subnet_resource_id:
            raise ValidationError('Infrastructure subnet resource ID needs to be supplied for internal only environments.')
        managed_env_def["properties"]["vnetConfiguration"]["internal"] = True

    try:
        r = ManagedEnvironmentClient.create(
            cmd=cmd, resource_group_name=resource_group_name, name=name, managed_environment_envelope=managed_env_def, no_wait=no_wait)

    except Exception as e:
        handle_raw_exception(e)

    _azure_monitor_quickstart(cmd, name, resource_group_name, storage_account, logs_destination)

    # return ENV
    if "properties" in r and "provisioningState" in r["properties"] and r["properties"]["provisioningState"].lower() != "succeeded" and not no_wait:
        not disable_warnings and logger.warning('Containerapp environment creation in progress. Please monitor the creation using `az containerapp env show -n {} -g {}`'.format(name, resource_group_name))

    if "properties" in r and "provisioningState" in r["properties"] and r["properties"]["provisioningState"].lower() == "succeeded":
        not disable_warnings and logger.warning("\nContainer Apps environment created. To deploy a container app, use: az containerapp create --help\n")

    return r


def update_managed_environment(cmd,
                               name,
                               resource_group_name,
                               logs_destination=None,
                               storage_account=None,
                               logs_customer_id=None,
                               logs_key=None,
                               hostname=None,
                               certificate_file=None,
                               certificate_password=None,
                               tags=None,
                               no_wait=False):
    if logs_destination == "log-analytics" or logs_customer_id or logs_key:
        if logs_destination != "log-analytics":
            raise ValidationError("When configuring Log Analytics workspace, --logs-destination should be \"log-analytics\"")
        if not logs_customer_id or not logs_key:
            raise ValidationError("Must provide --logs-workspace-id and --logs-workspace-key if updating logs destination to type 'log-analytics'.")

    try:
        r = ManagedEnvironmentClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except CLIError as e:
        handle_raw_exception(e)

    # General setup
    env_def = {}
    safe_set(env_def, "location", value=r["location"])  # required for API
    safe_set(env_def, "tags", value=tags)

    # Logs
    if logs_destination:
        logs_destination = None if logs_destination == "none" else logs_destination
        safe_set(env_def, "properties", "appLogsConfiguration", "destination", value=logs_destination)

    if logs_destination == "log-analytics":
        safe_set(env_def, "properties", "appLogsConfiguration", "logAnalyticsConfiguration", "customerId", value=logs_customer_id)
        safe_set(env_def, "properties", "appLogsConfiguration", "logAnalyticsConfiguration", "sharedKey", value=logs_key)
    elif logs_destination:
        safe_set(env_def, "properties", "appLogsConfiguration", "logAnalyticsConfiguration", value=None)

    # Custom domains
    if hostname:
        safe_set(env_def, "properties", "customDomainConfiguration", value={})
        cert_def = env_def["properties"]["customDomainConfiguration"]
        if certificate_file:
            blob, _ = load_cert_file(certificate_file, certificate_password)
            safe_set(cert_def, "certificateValue", value=blob)
        safe_set(cert_def, "dnsSuffix", value=hostname)
        if certificate_password:
            safe_set(cert_def, "certificatePassword", value=certificate_password)

    try:
        r = ManagedEnvironmentClient.update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, managed_environment_envelope=env_def, no_wait=no_wait)

    except Exception as e:
        handle_raw_exception(e)

    _azure_monitor_quickstart(cmd, name, resource_group_name, storage_account, logs_destination)

    return r


def show_managed_environment(cmd, name, resource_group_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        return ManagedEnvironmentClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except CLIError as e:
        handle_raw_exception(e)


def list_managed_environments(cmd, resource_group_name=None):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        managed_envs = []
        if resource_group_name is None:
            managed_envs = ManagedEnvironmentClient.list_by_subscription(cmd=cmd)
        else:
            managed_envs = ManagedEnvironmentClient.list_by_resource_group(cmd=cmd, resource_group_name=resource_group_name)

        return managed_envs
    except CLIError as e:
        handle_raw_exception(e)


def delete_managed_environment(cmd, name, resource_group_name, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        return ManagedEnvironmentClient.delete(cmd=cmd, name=name, resource_group_name=resource_group_name, no_wait=no_wait)
    except CLIError as e:
        handle_raw_exception(e)


def assign_managed_identity(cmd, name, resource_group_name, system_assigned=False, user_assigned=None, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)
    containerapp_def = None

    # Get containerapp properties of CA we are updating
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)
    set_managed_identity(cmd, resource_group_name, containerapp_def, system_assigned, user_assigned)

    try:
        r = ContainerAppClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)
        # If identity is not returned, do nothing
        return r["identity"]

    except Exception as e:
        handle_raw_exception(e)


def remove_managed_identity(cmd, name, resource_group_name, system_assigned=False, user_assigned=None, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    remove_system_identity = system_assigned
    remove_user_identities = user_assigned

    if user_assigned:
        remove_id_size = len(remove_user_identities)

        # Remove duplicate identities that are passed and notify
        remove_user_identities = list(set(remove_user_identities))
        if remove_id_size != len(remove_user_identities):
            logger.warning("At least one identity was passed twice.")

    containerapp_def = None
    # Get containerapp properties of CA we are updating
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)

    # If identity not returned
    try:
        containerapp_def["identity"]
        containerapp_def["identity"]["type"]
    except:
        containerapp_def["identity"] = {}
        containerapp_def["identity"]["type"] = "None"

    if containerapp_def["identity"]["type"] == "None":
        raise InvalidArgumentValueError("The containerapp {} has no system or user assigned identities.".format(name))

    if remove_system_identity:
        if containerapp_def["identity"]["type"] == "UserAssigned":
            raise InvalidArgumentValueError("The containerapp {} has no system assigned identities.".format(name))
        containerapp_def["identity"]["type"] = ("None" if containerapp_def["identity"]["type"] == "SystemAssigned" else "UserAssigned")

    if isinstance(user_assigned, list) and not user_assigned:
        containerapp_def["identity"]["userAssignedIdentities"] = {}
        remove_user_identities = []

        if containerapp_def["identity"]["userAssignedIdentities"] == {}:
            containerapp_def["identity"]["userAssignedIdentities"] = None
            containerapp_def["identity"]["type"] = ("None" if containerapp_def["identity"]["type"] == "UserAssigned" else "SystemAssigned")

    if remove_user_identities:
        subscription_id = get_subscription_id(cmd.cli_ctx)
        try:
            containerapp_def["identity"]["userAssignedIdentities"]
        except:
            containerapp_def["identity"]["userAssignedIdentities"] = {}
        for remove_id in remove_user_identities:
            given_id = remove_id
            remove_id = _ensure_identity_resource_id(subscription_id, resource_group_name, remove_id)
            wasRemoved = False

            for old_user_identity in containerapp_def["identity"]["userAssignedIdentities"]:
                if old_user_identity.lower() == remove_id.lower():
                    containerapp_def["identity"]["userAssignedIdentities"].pop(old_user_identity)
                    wasRemoved = True
                    break

            if not wasRemoved:
                raise InvalidArgumentValueError("The containerapp does not have specified user identity '{}' assigned, so it cannot be removed.".format(given_id))

        if containerapp_def["identity"]["userAssignedIdentities"] == {}:
            containerapp_def["identity"]["userAssignedIdentities"] = None
            containerapp_def["identity"]["type"] = ("None" if containerapp_def["identity"]["type"] == "UserAssigned" else "SystemAssigned")

    try:
        r = ContainerAppClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)
        return r["identity"]
    except Exception as e:
        handle_raw_exception(e)


def show_managed_identity(cmd, name, resource_group_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        r = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except CLIError as e:
        handle_raw_exception(e)

    try:
        return r["identity"]
    except:
        r["identity"] = {}
        r["identity"]["type"] = "None"
        return r["identity"]


def _validate_github(repo, branch, token):
    from github import Github, GithubException
    from github.GithubException import BadCredentialsException

    if repo:
        g = Github(token)
        github_repo = None
        try:
            github_repo = g.get_repo(repo)
            if not branch:
                branch = github_repo.default_branch
            if not github_repo.permissions.push or not github_repo.permissions.maintain:
                raise ValidationError("The token does not have appropriate access rights to repository {}.".format(repo))
            try:
                github_repo.get_branch(branch=branch)
            except GithubException as e:
                error_msg = "Encountered GitHub error when accessing {} branch in {} repo.".format(branch, repo)
                if e.data and e.data['message']:
                    error_msg += " Error: {}".format(e.data['message'])
                raise CLIInternalError(error_msg) from e
            logger.warning('Verified GitHub repo and branch')
        except BadCredentialsException as e:
            raise ValidationError("Could not authenticate to the repository. Please create a Personal Access Token and use "
                                  "the --token argument. Run 'az webapp deployment github-actions add --help' "
                                  "for more information.") from e
        except GithubException as e:
            error_msg = "Encountered GitHub error when accessing {} repo".format(repo)
            if e.data and e.data['message']:
                error_msg += " Error: {}".format(e.data['message'])
            raise CLIInternalError(error_msg) from e
    return branch


def create_or_update_github_action(cmd,
                                   name,
                                   resource_group_name,
                                   repo_url,
                                   registry_url=None,
                                   registry_username=None,
                                   registry_password=None,
                                   branch=None,
                                   token=None,
                                   login_with_github=False,
                                   image=None,
                                   context_path=None,
                                   service_principal_client_id=None,
                                   service_principal_client_secret=None,
                                   service_principal_tenant_id=None,
                                   no_wait=False):
    if not token and not login_with_github:
        raise_missing_token_suggestion()
    elif not token:
        scopes = ["admin:repo_hook", "repo", "workflow"]
        token = get_github_access_token(cmd, scopes)
    elif token and login_with_github:
        logger.warning("Both token and --login-with-github flag are provided. Will use provided token")

    repo = repo_url_to_name(repo_url)
    repo_url = f"https://github.com/{repo}"  # allow specifying repo as <user>/<repo> without the full github url

    branch = _validate_github(repo, branch, token)

    source_control_info = None

    try:
        source_control_info = GitHubActionClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)

    except Exception as ex:
        if not service_principal_client_id or not service_principal_client_secret or not service_principal_tenant_id:
            raise RequiredArgumentMissingError('Service principal client ID, secret and tenant ID are required to add github actions for the first time. Please create one using the command \"az ad sp create-for-rbac --name {{name}} --role contributor --scopes /subscriptions/{{subscription}}/resourceGroups/{{resourceGroup}} --sdk-auth\"') from ex
        source_control_info = SourceControlModel

    source_control_info["properties"]["repoUrl"] = repo_url
    source_control_info["properties"]["branch"] = branch

    azure_credentials = None

    if service_principal_client_id or service_principal_client_secret or service_principal_tenant_id:
        azure_credentials = AzureCredentialsModel
        azure_credentials["clientId"] = service_principal_client_id
        azure_credentials["clientSecret"] = service_principal_client_secret
        azure_credentials["tenantId"] = service_principal_tenant_id
        azure_credentials["subscriptionId"] = get_subscription_id(cmd.cli_ctx)

    # Registry
    if registry_username is None or registry_password is None:
        # If registry is Azure Container Registry, we can try inferring credentials
        if not registry_url or ACR_IMAGE_SUFFIX not in registry_url:
            raise RequiredArgumentMissingError('Registry url is required if using Azure Container Registry, otherwise Registry username and password are required if using Dockerhub')
        logger.warning('No credential was provided to access Azure Container Registry. Trying to look up...')
        parsed = urlparse(registry_url)
        registry_name = (parsed.netloc if parsed.scheme else parsed.path).split('.')[0]

        try:
            registry_username, registry_password, _ = _get_acr_cred(cmd.cli_ctx, registry_name)
        except Exception as ex:
            raise RequiredArgumentMissingError('Failed to retrieve credentials for container registry. Please provide the registry username and password') from ex

    registry_info = RegistryInfoModel
    registry_info["registryUrl"] = registry_url
    registry_info["registryUserName"] = registry_username
    registry_info["registryPassword"] = registry_password

    github_action_configuration = GitHubActionConfiguration
    github_action_configuration["registryInfo"] = registry_info
    github_action_configuration["azureCredentials"] = azure_credentials
    github_action_configuration["contextPath"] = context_path
    github_action_configuration["image"] = image

    source_control_info["properties"]["githubActionConfiguration"] = github_action_configuration

    headers = ["x-ms-github-auxiliary={}".format(token)]

    try:
        logger.warning("Creating Github action...")
        r = GitHubActionClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, name=name, github_action_envelope=source_control_info, headers=headers, no_wait=no_wait)
        if not no_wait:
            await_github_action(token, repo, r["properties"]["githubActionConfiguration"]["workflowName"])
        return r
    except Exception as e:
        handle_raw_exception(e)


def show_github_action(cmd, name, resource_group_name):
    try:
        return GitHubActionClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except Exception as e:
        handle_raw_exception(e)


def delete_github_action(cmd, name, resource_group_name, token=None, login_with_github=False):
    # Check if there is an existing source control to delete
    try:
        github_action_config = GitHubActionClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except Exception as e:
        handle_raw_exception(e)

    repo_url = github_action_config["properties"]["repoUrl"]

    if not token and not login_with_github:
        raise_missing_token_suggestion()
    elif not token:
        scopes = ["admin:repo_hook", "repo", "workflow"]
        token = get_github_access_token(cmd, scopes)
    elif token and login_with_github:
        logger.warning("Both token and --login-with-github flag are provided. Will use provided token")

    # Check if PAT can access repo
    try:
        # Verify github repo
        from github import Github, GithubException
        from github.GithubException import BadCredentialsException

        repo = None
        repo = repo_url.split('/')
        if len(repo) >= 2:
            repo = '/'.join(repo[-2:])

        if repo:
            g = Github(token)
            github_repo = None
            try:
                github_repo = g.get_repo(repo)
                if not github_repo.permissions.push or not github_repo.permissions.maintain:
                    raise ValidationError("The token does not have appropriate access rights to repository {}.".format(repo))
            except BadCredentialsException as e:
                raise CLIInternalError("Could not authenticate to the repository. Please create a Personal Access Token and use "
                                       "the --token argument. Run 'az webapp deployment github-actions add --help' "
                                       "for more information.") from e
            except GithubException as e:
                error_msg = "Encountered GitHub error when accessing {} repo".format(repo)
                if e.data and e.data['message']:
                    error_msg += " Error: {}".format(e.data['message'])
                raise CLIInternalError(error_msg) from e
    except CLIError as clierror:
        raise clierror
    except Exception:
        # If exception due to github package missing, etc just continue without validating the repo and rely on api validation
        pass

    headers = ["x-ms-github-auxiliary={}".format(token)]

    try:
        return GitHubActionClient.delete(cmd=cmd, resource_group_name=resource_group_name, name=name, headers=headers)
    except Exception as e:
        handle_raw_exception(e)


def list_revisions(cmd, name, resource_group_name, all=False):  # pylint: disable=redefined-builtin
    try:
        revision_list = ContainerAppClient.list_revisions(cmd=cmd, resource_group_name=resource_group_name, name=name)
        if all:
            return revision_list
        return [r for r in revision_list if r["properties"]["active"]]
    except CLIError as e:
        handle_raw_exception(e)


def show_revision(cmd, resource_group_name, revision_name, name=None):
    if not name:
        name = _get_app_from_revision(revision_name)

    try:
        return ContainerAppClient.show_revision(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, name=revision_name)
    except CLIError as e:
        handle_raw_exception(e)


def restart_revision(cmd, resource_group_name, revision_name, name=None):
    if not name:
        name = _get_app_from_revision(revision_name)

    try:
        return ContainerAppClient.restart_revision(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, name=revision_name)
    except CLIError as e:
        handle_raw_exception(e)


def activate_revision(cmd, resource_group_name, revision_name, name=None):
    if not name:
        name = _get_app_from_revision(revision_name)

    try:
        return ContainerAppClient.activate_revision(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, name=revision_name)
    except CLIError as e:
        handle_raw_exception(e)


def deactivate_revision(cmd, resource_group_name, revision_name, name=None):
    if not name:
        name = _get_app_from_revision(revision_name)

    try:
        return ContainerAppClient.deactivate_revision(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, name=revision_name)
    except CLIError as e:
        handle_raw_exception(e)


def copy_revision(cmd,
                  resource_group_name,
                  from_revision=None,
                  # label=None,
                  name=None,
                  yaml=None,
                  image=None,
                  container_name=None,
                  min_replicas=None,
                  max_replicas=None,
                  scale_rule_name=None,
                  scale_rule_type=None,
                  scale_rule_http_concurrency=None,
                  scale_rule_metadata=None,
                  scale_rule_auth=None,
                  set_env_vars=None,
                  replace_env_vars=None,
                  remove_env_vars=None,
                  remove_all_env_vars=False,
                  cpu=None,
                  memory=None,
                  revision_suffix=None,
                  startup_command=None,
                  args=None,
                  tags=None,
                  no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    if not name and not from_revision:
        raise RequiredArgumentMissingError('Usage error: --name is required if not using --from-revision.')

    if not name:
        name = _get_app_from_revision(from_revision)

    return update_containerapp_logic(cmd=cmd,
                                     name=name,
                                     resource_group_name=resource_group_name,
                                     yaml=yaml,
                                     image=image,
                                     container_name=container_name,
                                     min_replicas=min_replicas,
                                     max_replicas=max_replicas,
                                     scale_rule_name=scale_rule_name,
                                     scale_rule_type=scale_rule_type,
                                     scale_rule_http_concurrency=scale_rule_http_concurrency,
                                     scale_rule_metadata=scale_rule_metadata,
                                     scale_rule_auth=scale_rule_auth,
                                     set_env_vars=set_env_vars,
                                     remove_env_vars=remove_env_vars,
                                     replace_env_vars=replace_env_vars,
                                     remove_all_env_vars=remove_all_env_vars,
                                     cpu=cpu,
                                     memory=memory,
                                     revision_suffix=revision_suffix,
                                     startup_command=startup_command,
                                     args=args,
                                     tags=tags,
                                     no_wait=no_wait,
                                     from_revision=from_revision)


def set_revision_mode(cmd, resource_group_name, name, mode, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    containerapp_def["properties"]["configuration"]["activeRevisionsMode"] = mode.lower()

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)

    try:
        r = ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)
        return r["properties"]["configuration"]["activeRevisionsMode"]
    except Exception as e:
        handle_raw_exception(e)


def add_revision_label(cmd, resource_group_name, revision, label, name=None, no_wait=False, yes=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    if not name:
        name = _get_app_from_revision(revision)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError(f"The containerapp '{name}' does not exist in group '{resource_group_name}'")

    if "ingress" not in containerapp_def['properties']['configuration'] or "traffic" not in containerapp_def['properties']['configuration']['ingress']:
        raise ValidationError("Ingress and traffic weights are required to add labels.")

    traffic_weight = containerapp_def['properties']['configuration']['ingress']['traffic'] if 'traffic' in containerapp_def['properties']['configuration']['ingress'] else {}

    _validate_revision_name(cmd, revision, resource_group_name, name)

    label_added = False
    for weight in traffic_weight:
        if "label" in weight and weight["label"].lower() == label.lower():
            r_name = "latest" if "latestRevision" in weight and weight["latestRevision"] else weight["revisionName"]
            if not yes and r_name.lower() != revision.lower():
                msg = f"A weight with the label '{label}' already exists. Remove existing label '{label}' from '{r_name}' and add to '{revision}'?"
                if not prompt_y_n(msg, default="n"):
                    raise ArgumentUsageError(f"Usage Error: cannot specify existing label without agreeing to remove existing label '{label}' from '{r_name}' and add to '{revision}'.")
            weight["label"] = None

        if "latestRevision" in weight:
            if revision.lower() == "latest" and weight["latestRevision"]:
                label_added = True
                weight["label"] = label
        else:
            if revision.lower() == weight["revisionName"].lower():
                label_added = True
                weight["label"] = label

    if not label_added:
        containerapp_def["properties"]["configuration"]["ingress"]["traffic"].append({
            "revisionName": revision if revision.lower() != "latest" else None,
            "weight": 0,
            "latestRevision": revision.lower() == "latest",
            "label": label
        })

    containerapp_patch_def = {}
    containerapp_patch_def['properties'] = {}
    containerapp_patch_def['properties']['configuration'] = {}
    containerapp_patch_def['properties']['configuration']['ingress'] = {}

    containerapp_patch_def['properties']['configuration']['ingress']['traffic'] = traffic_weight

    try:
        r = ContainerAppClient.update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_patch_def, no_wait=no_wait)
        return r['properties']['configuration']['ingress']['traffic']
    except Exception as e:
        handle_raw_exception(e)


def swap_revision_label(cmd, name, resource_group_name, source_label, target_label, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    if source_label == target_label:
        raise ArgumentUsageError("Label names to be swapped must be different.")

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError(f"The containerapp '{name}' does not exist in group '{resource_group_name}'")

    if "ingress" not in containerapp_def['properties']['configuration'] or "traffic" not in containerapp_def['properties']['configuration']['ingress']:
        raise ValidationError("Ingress and traffic weights are required to swap labels.")

    traffic_weight = containerapp_def['properties']['configuration']['ingress']['traffic'] if 'traffic' in containerapp_def['properties']['configuration']['ingress'] else {}

    source_label_found = False
    target_label_found = False
    for weight in traffic_weight:
        if "label" in weight:
            if weight["label"].lower() == source_label.lower():
                if not source_label_found:
                    source_label_found = True
                    weight["label"] = target_label
            elif weight["label"].lower() == target_label.lower():
                if not target_label_found:
                    target_label_found = True
                    weight["label"] = source_label
    if not source_label_found and not target_label_found:
        raise ArgumentUsageError(f"Could not find label '{source_label}' nor label '{target_label}' in traffic.")
    if not source_label_found:
        raise ArgumentUsageError(f"Could not find label '{source_label}' in traffic.")
    if not target_label_found:
        raise ArgumentUsageError(f"Could not find label '{target_label}' in traffic.")

    containerapp_patch_def = {}
    containerapp_patch_def['properties'] = {}
    containerapp_patch_def['properties']['configuration'] = {}
    containerapp_patch_def['properties']['configuration']['ingress'] = {}

    containerapp_patch_def['properties']['configuration']['ingress']['traffic'] = traffic_weight

    try:
        r = ContainerAppClient.update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_patch_def, no_wait=no_wait)
        return r['properties']['configuration']['ingress']['traffic']
    except Exception as e:
        handle_raw_exception(e)


def remove_revision_label(cmd, resource_group_name, name, label, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError(f"The containerapp '{name}' does not exist in group '{resource_group_name}'")

    if "ingress" not in containerapp_def['properties']['configuration'] or "traffic" not in containerapp_def['properties']['configuration']['ingress']:
        raise ValidationError("Ingress and traffic weights are required to remove labels.")

    traffic_weight = containerapp_def['properties']['configuration']['ingress']['traffic']

    label_removed = False
    for weight in traffic_weight:
        if "label" in weight and weight["label"].lower() == label.lower():
            label_removed = True
            weight["label"] = None
            break
    if not label_removed:
        raise ValidationError("Please specify a label name with an associated traffic weight.")

    containerapp_patch_def = {}
    containerapp_patch_def['properties'] = {}
    containerapp_patch_def['properties']['configuration'] = {}
    containerapp_patch_def['properties']['configuration']['ingress'] = {}

    containerapp_patch_def['properties']['configuration']['ingress']['traffic'] = traffic_weight

    try:
        r = ContainerAppClient.update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_patch_def, no_wait=no_wait)
        return r['properties']['configuration']['ingress']['traffic']
    except Exception as e:
        handle_raw_exception(e)


def show_ingress(cmd, name, resource_group_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    try:
        return containerapp_def["properties"]["configuration"]["ingress"]
    except Exception as e:
        raise ValidationError("The containerapp '{}' does not have ingress enabled.".format(name)) from e


def enable_ingress(cmd, name, resource_group_name, type, target_port, transport="auto", exposed_port=None, allow_insecure=False, disable_warnings=False, no_wait=False):  # pylint: disable=redefined-builtin
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    external_ingress = None
    if type is not None:
        if type.lower() == "internal":
            external_ingress = False
        elif type.lower() == "external":
            external_ingress = True

    ingress_def = None
    if target_port is not None and type is not None:
        ingress_def = IngressModel
        ingress_def["external"] = external_ingress
        ingress_def["targetPort"] = target_port
        ingress_def["transport"] = transport
        ingress_def["allowInsecure"] = allow_insecure
        ingress_def["exposedPort"] = exposed_port if transport == "tcp" else None

    containerapp_def["properties"]["configuration"]["ingress"] = ingress_def

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)

    try:
        r = ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)
        not disable_warnings and logger.warning("\nIngress enabled. Access your app at https://{}/\n".format(r["properties"]["configuration"]["ingress"]["fqdn"]))
        return r["properties"]["configuration"]["ingress"]
    except Exception as e:
        handle_raw_exception(e)


def disable_ingress(cmd, name, resource_group_name, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    containerapp_def["properties"]["configuration"]["ingress"] = None

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)

    try:
        ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)
        logger.warning("Ingress has been disabled successfully.")
        return
    except Exception as e:
        handle_raw_exception(e)


def update_ingress(cmd, name, resource_group_name, type=None, target_port=None, transport=None, exposed_port=None, allow_insecure=False, disable_warnings=False, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    if containerapp_def["properties"]["configuration"]["ingress"] is None:
        raise ValidationError("The containerapp '{}' does not have ingress enabled. Try running `az containerapp ingress -h` for more info.".format(name))

    external_ingress = None
    if type is not None:
        if type.lower() == "internal":
            external_ingress = False
        elif type.lower() == "external":
            external_ingress = True

    containerapp_patch_def = {}
    containerapp_patch_def['properties'] = {}
    containerapp_patch_def['properties']['configuration'] = {}
    containerapp_patch_def['properties']['configuration']['ingress'] = {}

    ingress_def = {}
    if external_ingress is not None:
        ingress_def["external"] = external_ingress
    if target_port is not None:
        ingress_def["targetPort"] = target_port
    if transport is not None:
        ingress_def["transport"] = transport
    if allow_insecure is not None:
        ingress_def["allowInsecure"] = allow_insecure

    if "transport" in ingress_def and ingress_def["transport"] == "tcp":
        if exposed_port is not None:
            ingress_def["exposedPort"] = exposed_port
    else:
        ingress_def["exposedPort"] = None

    containerapp_patch_def["properties"]["configuration"]["ingress"] = ingress_def

    try:
        r = ContainerAppClient.update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_patch_def, no_wait=no_wait)
        not disable_warnings and logger.warning("\nIngress Updated. Access your app at https://{}/\n".format(r["properties"]["configuration"]["ingress"]["fqdn"]))
        return r["properties"]["configuration"]["ingress"]
    except Exception as e:
        handle_raw_exception(e)


def set_ingress_traffic(cmd, name, resource_group_name, label_weights=None, revision_weights=None, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)
    if not label_weights and not revision_weights:
        raise ValidationError("Must specify either --label-weight or --revision-weight.")

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError(f"The containerapp '{name}' does not exist in group '{resource_group_name}'")

    if containerapp_def["properties"]["configuration"]["activeRevisionsMode"].lower() == "single":
        raise ValidationError(f"Containerapp '{name}' is configured for single revision. Set revision mode to multiple in order to set ingress traffic. Try `az containerapp revision set-mode -h` for more details.")

    try:
        containerapp_def["properties"]["configuration"]["ingress"]
        containerapp_def["properties"]["configuration"]["ingress"]["traffic"]
    except Exception as e:
        raise ValidationError("Ingress must be enabled to set ingress traffic. Try running `az containerapp ingress -h` for more info.") from e

    if not revision_weights:
        revision_weights = []

    # convert label weights to appropriate revision name
    _append_label_weights(containerapp_def, label_weights, revision_weights)

    # validate sum is less than 100
    _validate_traffic_sum(revision_weights)

    # update revision weights to containerapp, get the old weight sum
    old_weight_sum = _update_revision_weights(containerapp_def, revision_weights)

    # validate revision names
    for revision in revision_weights:
        _validate_revision_name(cmd, revision.split('=')[0], resource_group_name, name)

    _update_weights(containerapp_def, revision_weights, old_weight_sum)

    containerapp_patch_def = {}
    containerapp_patch_def['properties'] = {}
    containerapp_patch_def['properties']['configuration'] = {}
    containerapp_patch_def['properties']['configuration']['ingress'] = {}
    containerapp_patch_def['properties']['configuration']['ingress']['traffic'] = containerapp_def["properties"]["configuration"]["ingress"]["traffic"]

    try:
        r = ContainerAppClient.update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_patch_def, no_wait=no_wait)
        return r['properties']['configuration']['ingress']['traffic']
    except Exception as e:
        handle_raw_exception(e)


def show_ingress_traffic(cmd, name, resource_group_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    try:
        return containerapp_def["properties"]["configuration"]["ingress"]["traffic"]
    except Exception as e:
        raise ValidationError("Ingress must be enabled to show ingress traffic. Try running `az containerapp ingress -h` for more info.") from e


def set_ip_restriction(cmd, name, resource_group_name, rule_name, ip_address, action, description=None, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError(f"The containerapp '{name}' does not exist in group '{resource_group_name}'")

    ip_restrictions = safe_get(containerapp_def, "properties", "configuration", "ingress", "ipSecurityRestrictions", default=[])

    ip_security_restrictions = set_ip_restrictions(ip_restrictions, rule_name, ip_address, description, action)
    containerapp_patch = {}
    safe_set(containerapp_patch, "properties", "configuration", "ingress", "ipSecurityRestrictions", value=ip_security_restrictions)
    try:
        r = ContainerAppClient.update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_patch, no_wait=no_wait)
        return r['properties']['configuration']['ingress']['ipSecurityRestrictions']
    except Exception as e:
        handle_raw_exception(e)


def remove_ip_restriction(cmd, name, resource_group_name, rule_name, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    ip_restrictions = safe_get(containerapp_def, "properties", "configuration", "ingress", "ipSecurityRestrictions", default=[])

    restriction_removed = False
    for index, value in enumerate(ip_restrictions):
        if value["name"].lower() == rule_name.lower():
            ip_restrictions.pop(index)
            restriction_removed = True
            break

    if not restriction_removed:
        raise ValidationError(f"Ip restriction name '{rule_name}' does not exist.")

    containerapp_patch = {}
    safe_set(containerapp_patch, "properties", "configuration", "ingress", "ipSecurityRestrictions", value=ip_restrictions)
    try:
        r = ContainerAppClient.update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_patch, no_wait=no_wait)
        ip_restrictions = safe_get(r, "properties", "configuration", "ingress", "ipSecurityRestrictions", default=[])
        return ip_restrictions
    except Exception as e:
        handle_raw_exception(e)


def show_ip_restrictions(cmd, name, resource_group_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    try:
        try:
            containerapp_def['properties']['configuration']['ingress']
        except Exception as e:
            raise ValidationError("Ingress must be enabled to list ip restrictions. Try running `az containerapp ingress -h` for more info.") from e
        return safe_get(containerapp_def, "properties", "configuration", "ingress", "ipSecurityRestrictions", default=[])
    except:
        return []


def show_registry(cmd, name, resource_group_name, server):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    try:
        containerapp_def["properties"]["configuration"]["registries"]
    except Exception as e:
        raise ValidationError("The containerapp {} has no assigned registries.".format(name)) from e

    registries_def = containerapp_def["properties"]["configuration"]["registries"]

    for r in registries_def:
        if r['server'].lower() == server.lower():
            return r
    raise InvalidArgumentValueError("The containerapp {} does not have specified registry assigned.".format(name))


def list_registry(cmd, name, resource_group_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    try:
        return containerapp_def["properties"]["configuration"]["registries"]
    except Exception as e:
        raise ValidationError("The containerapp {} has no assigned registries.".format(name)) from e


def set_registry(cmd, name, resource_group_name, server, username=None, password=None, disable_warnings=False, identity=None, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)
    if (username or password) and identity:
        raise MutuallyExclusiveArgumentError("Use either identity or username/password.")

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)

    registry = None

    registries_def = safe_get(containerapp_def, "properties", "configuration", "registries", default=[])
    containerapp_def["properties"]["configuration"]["registries"] = registries_def

    if (not username or not password) and not identity:
        # If registry is Azure Container Registry, we can try inferring credentials
        if ACR_IMAGE_SUFFIX not in server:
            raise RequiredArgumentMissingError('Registry username and password are required if you are not using Azure Container Registry.')
        not disable_warnings and logger.warning('No credential was provided to access Azure Container Registry. Trying to look up...')
        parsed = urlparse(server)
        registry_name = (parsed.netloc if parsed.scheme else parsed.path).split('.')[0]

        try:
            username, password, _ = _get_acr_cred(cmd.cli_ctx, registry_name)
        except Exception as ex:
            raise RequiredArgumentMissingError('Failed to retrieve credentials for container registry. Please provide the registry username and password') from ex

    # Check if updating existing registry
    updating_existing_registry = False
    for r in registries_def:
        if r['server'].lower() == server.lower():
            not disable_warnings and logger.warning("Updating existing registry.")
            updating_existing_registry = True
            if username:
                r["username"] = username
                r["identity"] = None
            if password:
                r["passwordSecretRef"] = store_as_secret_and_return_secret_ref(
                    containerapp_def["properties"]["configuration"]["secrets"],
                    r["username"],
                    r["server"],
                    password,
                    update_existing_secret=True)
                r["identity"] = None
            if identity:
                r["identity"] = identity
                r["username"] = None
                r["passwordSecretRef"] = None

    # If not updating existing registry, add as new registry
    if not updating_existing_registry:
        registry = RegistryCredentialsModel
        registry["server"] = server
        if not identity:
            registry["username"] = username
            registry["passwordSecretRef"] = store_as_secret_and_return_secret_ref(
                containerapp_def["properties"]["configuration"]["secrets"],
                username,
                server,
                password,
                update_existing_secret=True)
        else:
            registry["identity"] = identity

        registries_def.append(registry)

    if identity:
        system_assigned_identity = identity.lower() == "system"
        user_assigned = None if system_assigned_identity else [identity]
        set_managed_identity(cmd, resource_group_name, containerapp_def, system_assigned_identity, user_assigned)

    try:
        r = ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)

        return r["properties"]["configuration"]["registries"]
    except Exception as e:
        handle_raw_exception(e)


def remove_registry(cmd, name, resource_group_name, server, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)

    registries_def = None

    try:
        containerapp_def["properties"]["configuration"]["registries"]
    except Exception as e:
        raise ValidationError("The containerapp {} has no assigned registries.".format(name)) from e

    registries_def = containerapp_def["properties"]["configuration"]["registries"]

    wasRemoved = False
    for i, value in enumerate(registries_def):
        r = value
        if r['server'].lower() == server.lower():
            registries_def.pop(i)
            _remove_registry_secret(containerapp_def=containerapp_def, server=server, username=r["username"])
            wasRemoved = True
            break

    if not wasRemoved:
        raise ValidationError("Containerapp does not have registry server {} assigned.".format(server))

    if len(containerapp_def["properties"]["configuration"]["registries"]) == 0:
        containerapp_def["properties"]["configuration"].pop("registries")

    try:
        r = ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)
        logger.warning("Registry successfully removed.")
        return r["properties"]["configuration"]["registries"]
    # No registries to return, so return nothing
    except Exception:
        pass


def list_secrets(cmd, name, resource_group_name, show_values=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        r = containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    if not show_values:
        try:
            return r["properties"]["configuration"]["secrets"]
        except:
            return []
    try:
        return ContainerAppClient.list_secrets(cmd=cmd, resource_group_name=resource_group_name, name=name)["value"]
    except Exception:
        return []
        # raise ValidationError("The containerapp {} has no assigned secrets.".format(name)) from e


def show_secret(cmd, name, resource_group_name, secret_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    r = ContainerAppClient.list_secrets(cmd=cmd, resource_group_name=resource_group_name, name=name)
    for secret in r["value"]:
        if secret["name"].lower() == secret_name.lower():
            return secret
    raise ValidationError("The containerapp {} does not have a secret assigned with name {}.".format(name, secret_name))


def remove_secrets(cmd, name, resource_group_name, secret_names, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)

    for secret_name in secret_names:
        wasRemoved = False
        for secret in containerapp_def["properties"]["configuration"]["secrets"]:
            if secret["name"].lower() == secret_name.lower():
                _remove_secret(containerapp_def, secret_name=secret["name"])
                wasRemoved = True
                break
        if not wasRemoved:
            raise ValidationError("The containerapp {} does not have a secret assigned with name {}.".format(name, secret_name))
    try:
        r = ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)
        logger.warning("Secret(s) successfully removed.")
        try:
            return r["properties"]["configuration"]["secrets"]
        # No secrets to return
        except:
            pass
    except Exception as e:
        handle_raw_exception(e)


def set_secrets(cmd, name, resource_group_name, secrets,
                # yaml=None,
                disable_max_length=False,
                no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    for s in secrets:
        if s:
            parsed = s.split("=")
            if parsed:
                if len(parsed[0]) > MAXIMUM_SECRET_LENGTH and not disable_max_length:
                    raise ValidationError(f"Secret names cannot be longer than {MAXIMUM_SECRET_LENGTH}. "
                                          f"Please shorten {parsed[0]}")

    # if not yaml and not secrets:
    #     raise RequiredArgumentMissingError('Usage error: --secrets is required if not using --yaml')

    # if not secrets:
    #     secrets = []

    # if yaml:
    #     yaml_secrets = load_yaml_file(yaml).split(' ')
    #     try:
    #         parse_secret_flags(yaml_secrets)
    #     except:
    #         raise ValidationError("YAML secrets must be a list of secrets in key=value format, delimited by new line.")
    #     for secret in yaml_secrets:
    #         secrets.append(secret.strip())

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)
    _add_or_update_secrets(containerapp_def, parse_secret_flags(secrets))

    try:
        r = ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)
        logger.warning("Containerapp '{}' must be restarted in order for secret changes to take effect.".format(name))
        return r["properties"]["configuration"]["secrets"]
    except Exception as e:
        handle_raw_exception(e)


def enable_dapr(cmd, name, resource_group_name,
                dapr_app_id=None,
                dapr_app_port=None,
                dapr_app_protocol=None,
                dapr_http_read_buffer_size=None,
                dapr_http_max_request_size=None,
                dapr_log_level=None,
                dapr_enable_api_logging=False,
                no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)

    if 'configuration' not in containerapp_def['properties']:
        containerapp_def['properties']['configuration'] = {}

    if not safe_get(containerapp_def['properties']['configuration'], 'dapr'):
        containerapp_def['properties']['configuration']['dapr'] = {}

    if dapr_app_id:
        containerapp_def['properties']['configuration']['dapr']['appId'] = dapr_app_id

    if dapr_app_port:
        containerapp_def['properties']['configuration']['dapr']['appPort'] = dapr_app_port

    if dapr_app_protocol:
        containerapp_def['properties']['configuration']['dapr']['appProtocol'] = dapr_app_protocol

    if dapr_http_read_buffer_size:
        containerapp_def['properties']['configuration']['dapr']['httpReadBufferSize'] = dapr_http_read_buffer_size

    if dapr_http_max_request_size:
        containerapp_def['properties']['configuration']['dapr']['httpMaxRequestSize'] = dapr_http_max_request_size

    if dapr_log_level:
        containerapp_def['properties']['configuration']['dapr']['logLevel'] = dapr_log_level

    if dapr_enable_api_logging:
        containerapp_def['properties']['configuration']['dapr']['enableApiLogging'] = dapr_enable_api_logging

    containerapp_def['properties']['configuration']['dapr']['enabled'] = True

    try:
        r = ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)
        return r["properties"]['configuration']['dapr']
    except Exception as e:
        handle_raw_exception(e)


def disable_dapr(cmd, name, resource_group_name, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if not containerapp_def:
        raise ResourceNotFoundError("The containerapp '{}' does not exist".format(name))

    _get_existing_secrets(cmd, resource_group_name, name, containerapp_def)

    if 'configuration' not in containerapp_def['properties']:
        containerapp_def['properties']['configuration'] = {}

    if 'dapr' not in containerapp_def['properties']['configuration']:
        containerapp_def['properties']['configuration']['dapr'] = {}

    containerapp_def['properties']['configuration']['dapr']['enabled'] = False

    try:
        r = ContainerAppClient.create_or_update(
            cmd=cmd, resource_group_name=resource_group_name, name=name, container_app_envelope=containerapp_def, no_wait=no_wait)
        return r["properties"]['configuration']['dapr']
    except Exception as e:
        handle_raw_exception(e)


def list_dapr_components(cmd, resource_group_name, environment_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    return DaprComponentClient.list(cmd, resource_group_name, environment_name)


def show_dapr_component(cmd, resource_group_name, dapr_component_name, environment_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    return DaprComponentClient.show(cmd, resource_group_name, environment_name, name=dapr_component_name)


def create_or_update_dapr_component(cmd, resource_group_name, environment_name, dapr_component_name, yaml):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    yaml_containerapp = load_yaml_file(yaml)
    if type(yaml_containerapp) != dict:  # pylint: disable=unidiomatic-typecheck
        raise ValidationError('Invalid YAML provided. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.')

    # Deserialize the yaml into a DaprComponent object. Need this since we're not using SDK
    daprcomponent_def = None
    try:
        deserializer = create_deserializer()

        daprcomponent_def = deserializer('DaprComponent', yaml_containerapp)
    except DeserializationError as ex:
        raise ValidationError('Invalid YAML provided. Please see https://aka.ms/azure-container-apps-yaml for a valid containerapps YAML spec.') from ex

    daprcomponent_def = _convert_object_from_snake_to_camel_case(_object_to_dict(daprcomponent_def))

    # Remove "additionalProperties" and read-only attributes that are introduced in the deserialization. Need this since we're not using SDK
    _remove_additional_attributes(daprcomponent_def)
    _remove_dapr_readonly_attributes(daprcomponent_def)

    if not daprcomponent_def["ignoreErrors"]:
        daprcomponent_def["ignoreErrors"] = False

    dapr_component_envelope = {}

    dapr_component_envelope["properties"] = daprcomponent_def

    try:
        r = DaprComponentClient.create_or_update(cmd, resource_group_name=resource_group_name, environment_name=environment_name, dapr_component_envelope=dapr_component_envelope, name=dapr_component_name)
        return r
    except Exception as e:
        handle_raw_exception(e)


def remove_dapr_component(cmd, resource_group_name, dapr_component_name, environment_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        DaprComponentClient.show(cmd, resource_group_name, environment_name, name=dapr_component_name)
    except Exception as e:
        raise ResourceNotFoundError("Dapr component not found.") from e

    try:
        r = DaprComponentClient.delete(cmd, resource_group_name, environment_name, name=dapr_component_name)
        logger.warning("Dapr componenet successfully deleted.")
        return r
    except Exception as e:
        handle_raw_exception(e)


def list_replicas(cmd, resource_group_name, name, revision=None):
    app = ContainerAppClient.show(cmd, resource_group_name, name)
    if not revision:
        revision = app["properties"]["latestRevisionName"]
    return ContainerAppClient.list_replicas(cmd=cmd,
                                            resource_group_name=resource_group_name,
                                            container_app_name=name,
                                            revision_name=revision)


def get_replica(cmd, resource_group_name, name, replica, revision=None):
    app = ContainerAppClient.show(cmd, resource_group_name, name)
    if not revision:
        revision = app["properties"]["latestRevisionName"]
    return ContainerAppClient.get_replica(cmd=cmd,
                                          resource_group_name=resource_group_name,
                                          container_app_name=name,
                                          revision_name=revision,
                                          replica_name=replica)


def containerapp_ssh(cmd, resource_group_name, name, container=None, revision=None, replica=None, startup_command="sh"):
    if isinstance(startup_command, list):
        startup_command = startup_command[0]  # CLI seems a little buggy when calling a param "--command"

    conn = WebSocketConnection(cmd=cmd, resource_group_name=resource_group_name, name=name, revision=revision,
                               replica=replica, container=container, startup_command=startup_command)

    encodings = [SSH_DEFAULT_ENCODING, SSH_BACKUP_ENCODING]
    reader = threading.Thread(target=read_ssh, args=(conn, encodings))
    reader.daemon = True
    reader.start()

    writer = get_stdin_writer(conn)
    writer.daemon = True
    writer.start()

    logger.warning("Use ctrl + D to exit.")
    while conn.is_connected:
        try:
            time.sleep(0.1)
        except KeyboardInterrupt:
            if conn.is_connected:
                logger.info("Caught KeyboardInterrupt. Sending ctrl+c to server")
                conn.send(SSH_CTRL_C_MSG)


def stream_containerapp_logs(cmd, resource_group_name, name, container=None, revision=None, replica=None, follow=False,
                             tail=None, output_format=None, kind=None):
    if tail:
        if tail < 0 or tail > 300:
            raise ValidationError("--tail must be between 0 and 300.")
    if kind == LOG_TYPE_SYSTEM:
        if container or replica or revision:
            raise MutuallyExclusiveArgumentError("--type: --container, --replica, and --revision not supported for system logs")
        if output_format and output_format != "json":
            raise MutuallyExclusiveArgumentError("--type: only json logs supported for system logs")

    sub = get_subscription_id(cmd.cli_ctx)
    token_response = ContainerAppClient.get_auth_token(cmd, resource_group_name, name)
    token = token_response["properties"]["token"]

    base_url = ContainerAppClient.show(cmd, resource_group_name, name)["properties"]["eventStreamEndpoint"]
    base_url = base_url[:base_url.index("/subscriptions/")]

    if kind == LOG_TYPE_CONSOLE:
        url = (f"{base_url}/subscriptions/{sub}/resourceGroups/{resource_group_name}/containerApps/{name}"
               f"/revisions/{revision}/replicas/{replica}/containers/{container}/logstream")
    else:
        url = f"{base_url}/subscriptions/{sub}/resourceGroups/{resource_group_name}/containerApps/{name}/eventstream"

    logger.info("connecting to : %s", url)
    request_params = {"follow": str(follow).lower(),
                      "output": output_format,
                      "tailLines": tail}
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url,
                        timeout=None,
                        stream=True,
                        params=request_params,
                        headers=headers)

    if not resp.ok:
        ValidationError(f"Got bad status from the logstream API: {resp.status_code}")

    for line in resp.iter_lines():
        if line:
            logger.info("received raw log line: %s", line)
            # these .replaces are needed to display color/quotations properly
            # for some reason the API returns garbled unicode special characters (may need to add more in the future)
            print(line.decode("utf-8").replace("\\u0022", "\u0022").replace("\\u001B", "\u001B").replace("\\u002B", "\u002B").replace("\\u0027", "\u0027"))


def stream_environment_logs(cmd, resource_group_name, name, follow=False, tail=None):
    if tail:
        if tail < 0 or tail > 300:
            raise ValidationError("--tail must be between 0 and 300.")

    env = show_managed_environment(cmd, name, resource_group_name)
    sub = get_subscription_id(cmd.cli_ctx)
    token_response = ManagedEnvironmentClient.get_auth_token(cmd, resource_group_name, name)
    token = token_response["properties"]["token"]
    base_url = f"https://{env['location']}.azurecontainerapps.dev"

    url = (f"{base_url}/subscriptions/{sub}/resourceGroups/{resource_group_name}/managedEnvironments/{name}"
           f"/eventstream")

    logger.info("connecting to : %s", url)
    request_params = {"follow": str(follow).lower(),
                      "tailLines": tail}
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url,
                        timeout=None,
                        stream=True,
                        params=request_params,
                        headers=headers)

    if not resp.ok:
        ValidationError(f"Got bad status from the logstream API: {resp.status_code}")

    for line in resp.iter_lines():
        if line:
            logger.info("received raw log line: %s", line)
            # these .replaces are needed to display color/quotations properly
            # for some reason the API returns garbled unicode special characters (may need to add more in the future)
            print(line.decode("utf-8").replace("\\u0022", "\u0022").replace("\\u001B", "\u001B").replace("\\u002B", "\u002B").replace("\\u0027", "\u0027"))


def open_containerapp_in_browser(cmd, name, resource_group_name):
    app = ContainerAppClient.show(cmd, resource_group_name, name)
    url = safe_get(app, "properties", "configuration", "ingress", "fqdn")
    if not url:
        raise ValidationError("Could not open in browser: no public URL for this app")
    if not url.startswith("http"):
        url = f"http://{url}"
    open_page_in_browser(url)


def containerapp_up(cmd,
                    name,
                    resource_group_name=None,
                    managed_env=None,
                    location=None,
                    registry_server=None,
                    image=None,
                    source=None,
                    ingress=None,
                    target_port=None,
                    registry_user=None,
                    registry_pass=None,
                    env_vars=None,
                    logs_customer_id=None,
                    logs_key=None,
                    repo=None,
                    token=None,
                    branch=None,
                    browse=False,
                    context_path=None,
                    service_principal_client_id=None,
                    service_principal_client_secret=None,
                    service_principal_tenant_id=None):
    from ._up_utils import (_validate_up_args, _reformat_image, _get_dockerfile_content, _get_ingress_and_target_port,
                            ResourceGroup, ContainerAppEnvironment, ContainerApp, _get_registry_from_app,
                            _get_registry_details, _create_github_action, _set_up_defaults, up_output,
                            check_env_name_on_rg, get_token, _validate_containerapp_name, _has_dockerfile)
    from ._github_oauth import cache_github_token
    HELLOWORLD = "mcr.microsoft.com/azuredocs/containerapps-helloworld"
    dockerfile = "Dockerfile"  # for now the dockerfile name must be "Dockerfile" (until GH actions API is updated)

    _validate_containerapp_name(name)

    register_provider_if_needed(cmd, CONTAINER_APPS_RP)
    _validate_up_args(cmd, source, image, repo, registry_server)
    validate_container_app_name(name)
    check_env_name_on_rg(cmd, managed_env, resource_group_name, location)

    image = _reformat_image(source, repo, image)
    token = get_token(cmd, repo, token)

    if image and HELLOWORLD in image.lower():
        ingress = "external" if not ingress else ingress
        target_port = 80 if not target_port else target_port

    if image:
        if ingress and not target_port:
            target_port = 80
            logger.warning("No ingress provided, defaulting to port 80. Try `az containerapp up --ingress %s --target-port <port>` to set a custom port.", ingress)

    if source and not _has_dockerfile(source, dockerfile):
        pass
    else:
        dockerfile_content = _get_dockerfile_content(repo, branch, token, source, context_path, dockerfile)
        ingress, target_port = _get_ingress_and_target_port(ingress, target_port, dockerfile_content)

    resource_group = ResourceGroup(cmd, name=resource_group_name, location=location)
    env = ContainerAppEnvironment(cmd, managed_env, resource_group, location=location, logs_key=logs_key, logs_customer_id=logs_customer_id)
    app = ContainerApp(cmd, name, resource_group, None, image, env, target_port, registry_server, registry_user, registry_pass, env_vars, ingress)

    _set_up_defaults(cmd, name, resource_group_name, logs_customer_id, location, resource_group, env, app)

    if app.check_exists():
        if app.get()["properties"]["provisioningState"] == "InProgress":
            raise ValidationError("Containerapp has an existing provisioning in progress. Please wait until provisioning has completed and rerun the command.")

    resource_group.create_if_needed()
    env.create_if_needed(name)

    if source or repo:
        if not registry_server:
            _get_registry_from_app(app, source)  # if the app exists, get the registry
        _get_registry_details(cmd, app, source)  # fetch ACR creds from arguments registry arguments

    app.create_acr_if_needed()

    if source:
        app.run_acr_build(dockerfile, source, quiet=False, build_from_source=not _has_dockerfile(source, dockerfile))

    app.create(no_registry=bool(repo))
    if repo:
        _create_github_action(app, env, service_principal_client_id, service_principal_client_secret,
                              service_principal_tenant_id, branch, token, repo, context_path)
        cache_github_token(cmd, token, repo)

    if browse:
        open_containerapp_in_browser(cmd, app.name, app.resource_group.name)

    up_output(app, no_dockerfile=(source and not _has_dockerfile(source, dockerfile)))


def containerapp_up_logic(cmd, resource_group_name, name, managed_env, image, env_vars, ingress, target_port, registry_server, registry_user, registry_pass):
    containerapp_def = None
    try:
        containerapp_def = ContainerAppClient.show(cmd=cmd, resource_group_name=resource_group_name, name=name)
    except:
        pass

    if containerapp_def:
        return update_containerapp_logic(cmd=cmd, name=name, resource_group_name=resource_group_name, image=image, replace_env_vars=env_vars, ingress=ingress, target_port=target_port, registry_server=registry_server, registry_user=registry_user, registry_pass=registry_pass, container_name=name)
    return create_containerapp(cmd=cmd, name=name, resource_group_name=resource_group_name, managed_env=managed_env, image=image, env_vars=env_vars, ingress=ingress, target_port=target_port, registry_server=registry_server, registry_user=registry_user, registry_pass=registry_pass)


def create_managed_certificate(cmd, name, resource_group_name, hostname, validation_method, certificate_name=None):
    if certificate_name and not check_managed_cert_name_availability(cmd, resource_group_name, name, certificate_name):
        raise ValidationError(f"Certificate name '{certificate_name}' is not available.")
    cert_name = certificate_name
    while not cert_name:
        cert_name = generate_randomized_managed_cert_name(hostname, resource_group_name)
        if not check_managed_cert_name_availability(cmd, resource_group_name, name, certificate_name):
            cert_name = None
    certificate_envelop = prepare_managed_certificate_envelop(cmd, name, resource_group_name, hostname, validation_method)
    try:
        r = ManagedEnvironmentClient.create_or_update_managed_certificate(cmd, resource_group_name, name, cert_name, certificate_envelop, True, validation_method == 'TXT')
        return r
    except Exception as e:
        handle_raw_exception(e)


def list_certificates(cmd, name, resource_group_name, location=None, certificate=None, thumbprint=None, managed_certificates_only=False, private_key_certificates_only=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)
    if managed_certificates_only and private_key_certificates_only:
        raise MutuallyExclusiveArgumentError("Use either '--managed-certificates-only' or '--private-key-certificates-only'.")
    if managed_certificates_only and thumbprint:
        raise MutuallyExclusiveArgumentError("'--thumbprint' not supported for managed certificates.")

    if certificate and is_valid_resource_id(certificate):
        certificate_name = parse_resource_id(certificate)["resource_name"]
        certificate_type = parse_resource_id(certificate)["resource_type"]
    else:
        certificate_name = certificate
        certificate_type = PRIVATE_CERTIFICATE_RT if private_key_certificates_only or thumbprint else (MANAGED_CERTIFICATE_RT if managed_certificates_only else None)

    if certificate_type == MANAGED_CERTIFICATE_RT:
        return get_managed_certificates(cmd, name, resource_group_name, certificate_name, location)
    if certificate_type == PRIVATE_CERTIFICATE_RT:
        return get_private_certificates(cmd, name, resource_group_name, certificate_name, thumbprint, location)
    managed_certs = get_managed_certificates(cmd, name, resource_group_name, certificate_name, location)
    private_certs = get_private_certificates(cmd, name, resource_group_name, certificate_name, thumbprint, location)
    return managed_certs + private_certs


def get_private_certificates(cmd, name, resource_group_name, certificate_name=None, thumbprint=None, location=None):
    if certificate_name:
        try:
            r = ManagedEnvironmentClient.show_certificate(cmd, resource_group_name, name, certificate_name)
            return [r] if certificate_matches(r, location, thumbprint) else []
        except Exception as e:
            handle_non_404_exception(e)
            return []
    else:
        try:
            r = ManagedEnvironmentClient.list_certificates(cmd, resource_group_name, name)
            return list(filter(lambda c: certificate_matches(c, location, thumbprint), r))
        except Exception as e:
            handle_raw_exception(e)


def get_managed_certificates(cmd, name, resource_group_name, certificate_name=None, location=None):
    if certificate_name:
        try:
            r = ManagedEnvironmentClient.show_managed_certificate(cmd, resource_group_name, name, certificate_name)
            return [r] if certificate_location_matches(r, location) else []
        except Exception as e:
            handle_non_404_exception(e)
            return []
    else:
        try:
            r = ManagedEnvironmentClient.list_managed_certificates(cmd, resource_group_name, name)
            return list(filter(lambda c: certificate_location_matches(c, location), r))
        except Exception as e:
            handle_raw_exception(e)


def upload_certificate(cmd, name, resource_group_name, certificate_file, certificate_name=None, certificate_password=None, location=None, prompt=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    blob, thumbprint = load_cert_file(certificate_file, certificate_password)

    cert_name = None
    if certificate_name:
        name_availability = check_cert_name_availability(cmd, resource_group_name, name, certificate_name)
        if not name_availability["nameAvailable"]:
            if name_availability["reason"] == NAME_ALREADY_EXISTS:
                msg = '{}. If continue with this name, it will be overwritten by the new certificate file.\nOverwrite?'
                overwrite = True
                if prompt:
                    overwrite = prompt_y_n(msg.format(name_availability["message"]))
                else:
                    logger.warning('{}. It will be overwritten by the new certificate file.'.format(name_availability["message"]))
                if overwrite:
                    cert_name = certificate_name
            else:
                raise ValidationError(name_availability["message"])
        else:
            cert_name = certificate_name

    while not cert_name:
        random_name = generate_randomized_cert_name(thumbprint, name, resource_group_name)
        check_result = check_cert_name_availability(cmd, resource_group_name, name, random_name)
        if check_result["nameAvailable"]:
            cert_name = random_name
        elif not check_result["nameAvailable"] and (check_result["reason"] == NAME_INVALID):
            raise ValidationError(check_result["message"])

    certificate = ContainerAppCertificateEnvelopeModel
    certificate["properties"]["password"] = certificate_password
    certificate["properties"]["value"] = blob
    certificate["location"] = location
    if not certificate["location"]:
        try:
            managed_env = ManagedEnvironmentClient.show(cmd, resource_group_name, name)
            certificate["location"] = managed_env["location"]
        except Exception as e:
            handle_raw_exception(e)

    try:
        r = ManagedEnvironmentClient.create_or_update_certificate(cmd, resource_group_name, name, cert_name, certificate)
        return r
    except Exception as e:
        handle_raw_exception(e)


def delete_certificate(cmd, resource_group_name, name, location=None, certificate=None, thumbprint=None):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    if not certificate and not thumbprint:
        raise RequiredArgumentMissingError('Please specify at least one of parameters: --certificate and --thumbprint')

    cert_type = None
    cert_name = certificate
    if certificate and is_valid_resource_id(certificate):
        cert_type = parse_resource_id(certificate)["resource_type"]
        cert_name = parse_resource_id(certificate)["resource_name"]
    if thumbprint:
        cert_type = PRIVATE_CERTIFICATE_RT

    if cert_type == PRIVATE_CERTIFICATE_RT:
        certs = list_certificates(cmd, name, resource_group_name, location, certificate, thumbprint)
        if len(certs) == 0:
            msg = "'{}'".format(cert_name) if cert_name else "with thumbprint '{}'".format(thumbprint)
            raise ResourceNotFoundError(f"The certificate {msg} does not exist in Container app environment '{name}'.")
        for cert in certs:
            try:
                ManagedEnvironmentClient.delete_certificate(cmd, resource_group_name, name, cert["name"])
                logger.warning('Successfully deleted certificate: %s', cert["name"])
            except Exception as e:
                handle_raw_exception(e)
    elif cert_type == MANAGED_CERTIFICATE_RT:
        try:
            ManagedEnvironmentClient.delete_managed_certificate(cmd, resource_group_name, name, cert_name)
            logger.warning('Successfully deleted certificate: {}'.format(cert_name))
        except Exception as e:
            handle_raw_exception(e)
    else:
        managed_certs = list(filter(lambda c: c["name"] == cert_name, get_managed_certificates(cmd, name, resource_group_name, None, location)))
        private_certs = list(filter(lambda c: c["name"] == cert_name, get_private_certificates(cmd, name, resource_group_name, None, None, location)))
        if len(managed_certs) == 0 and len(private_certs) == 0:
            raise ResourceNotFoundError(f"The certificate '{cert_name}' does not exist in Container app environment '{name}'.")
        if len(managed_certs) > 0 and len(private_certs) > 0:
            raise RequiredArgumentMissingError(f"Found more than one certificates with name '{cert_name}':\n'{managed_certs[0]['id']}',\n'{private_certs[0]['id']}'.\nPlease specify the certificate id using --certificate.")
        try:
            ManagedEnvironmentClient.delete_managed_certificate(cmd, resource_group_name, name, cert_name)
            logger.warning('Successfully deleted certificate: %s', cert_name)
        except Exception as e:
            handle_raw_exception(e)


def upload_ssl(cmd, resource_group_name, name, environment, certificate_file, hostname, certificate_password=None, certificate_name=None, location=None):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    passed, message = validate_hostname(cmd, resource_group_name, name, hostname)
    if not passed:
        raise ValidationError(message or 'Please configure the DNS records before adding the hostname.')

    custom_domains = get_custom_domains(cmd, resource_group_name, name, location, environment)
    new_custom_domains = list(filter(lambda c: c["name"] != hostname, custom_domains))

    env_name = _get_name(environment)
    logger.warning('Uploading certificate to %s.', env_name)
    if is_valid_resource_id(environment):
        cert = upload_certificate(cmd, env_name, parse_resource_id(environment)["resource_group"], certificate_file, certificate_name, certificate_password, location)
    else:
        cert = upload_certificate(cmd, env_name, resource_group_name, certificate_file, certificate_name, certificate_password, location)
    cert_id = cert["id"]

    new_domain = ContainerAppCustomDomainModel
    new_domain["name"] = hostname
    new_domain["certificateId"] = cert_id
    new_custom_domains.append(new_domain)
    logger.warning('Adding hostname %s and binding to %s.', hostname, name)
    return patch_new_custom_domain(cmd, resource_group_name, name, new_custom_domains)


def bind_hostname(cmd, resource_group_name, name, hostname, thumbprint=None, certificate=None, location=None, environment=None, validation_method=None):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    if not environment and not certificate:
        raise RequiredArgumentMissingError('Please specify at least one of parameters: --certificate and --environment')
    if certificate and not is_valid_resource_id(certificate) and not environment:
        raise RequiredArgumentMissingError('Please specify the parameter: --environment')

    standardized_hostname = hostname.lower()
    passed, message = validate_hostname(cmd, resource_group_name, name, standardized_hostname)
    if not passed:
        raise ValidationError(message or 'Please configure the DNS records before adding the hostname.')

    env_name = _get_name(environment) if environment else None

    if certificate:
        if is_valid_resource_id(certificate):
            cert_id = certificate
        else:
            certs = list_certificates(cmd, env_name, resource_group_name, location, certificate, thumbprint)
            if len(certs) == 0:
                msg = "'{}' with thumbprint '{}'".format(certificate, thumbprint) if thumbprint else "'{}'".format(certificate)
                raise ResourceNotFoundError(f"The certificate {msg} does not exist in Container app environment '{env_name}'.")
            cert_id = certs[0]["id"]
    elif thumbprint:
        certs = list_certificates(cmd, env_name, resource_group_name, location, certificate, thumbprint)
        if len(certs) == 0:
            raise ResourceNotFoundError(f"The certificate with thumbprint '{thumbprint}' does not exist in Container app environment '{env_name}'.")
        cert_id = certs[0]["id"]
    else:  # look for or create a managed certificate if no certificate info provided
        managed_certs = get_managed_certificates(cmd, env_name, resource_group_name, None, None)
        managed_cert = [cert for cert in managed_certs if cert["properties"]["subjectName"].lower() == standardized_hostname]
        if len(managed_cert) > 0 and managed_cert[0]["properties"]["provisioningState"] in [SUCCEEDED_STATUS, PENDING_STATUS]:
            cert_id = managed_cert[0]["id"]
            cert_name = managed_cert[0]["name"]
        else:
            cert_name = None
            while not cert_name:
                random_name = generate_randomized_managed_cert_name(standardized_hostname, env_name)
                available = check_managed_cert_name_availability(cmd, resource_group_name, env_name, cert_name)
                if available:
                    cert_name = random_name
            logger.warning("Creating managed certificate '%s' for %s.\nIt may take up to 20 minutes to create and issue a managed certificate.", cert_name, standardized_hostname)

            validation = validation_method
            while validation not in ["TXT", "CNAME", "HTTP"]:
                validation = prompt_str('\nPlease choose one of the following domain validation methods: TXT, CNAME, HTTP\nYour answer: ')

            certificate_envelop = prepare_managed_certificate_envelop(cmd, env_name, resource_group_name, standardized_hostname, validation_method, location)
            try:
                managed_cert = ManagedEnvironmentClient.create_or_update_managed_certificate(cmd, resource_group_name, env_name, cert_name, certificate_envelop, False, validation_method == 'TXT')
            except Exception as e:
                handle_raw_exception(e)
            cert_id = managed_cert["id"]

        logger.warning("\nBinding managed certificate '%s' to %s\n", cert_name, standardized_hostname)

    custom_domains = get_custom_domains(cmd, resource_group_name, name, location, environment)
    new_custom_domains = list(filter(lambda c: safe_get(c, "name", default=[]) != standardized_hostname, custom_domains))
    new_domain = ContainerAppCustomDomainModel
    new_domain["name"] = standardized_hostname
    new_domain["certificateId"] = cert_id
    new_custom_domains.append(new_domain)

    return patch_new_custom_domain(cmd, resource_group_name, name, new_custom_domains)


def add_hostname(cmd, resource_group_name, name, hostname, location=None):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)
    standardized_hostname = hostname.lower()
    custom_domains = get_custom_domains(cmd, resource_group_name, name, location, None)
    existing_hostname = list(filter(lambda c: safe_get(c, "name", default=[]) == standardized_hostname, custom_domains))
    if len(existing_hostname) > 0:
        raise InvalidArgumentValueError("'{standardized_hostname}' already exists in container app '{name}'.")
    new_domain = ContainerAppCustomDomainModel
    new_domain["name"] = standardized_hostname
    new_domain["bindingType"] = "Disabled"
    custom_domains.append(new_domain)
    return patch_new_custom_domain(cmd, resource_group_name, name, custom_domains)


def list_hostname(cmd, resource_group_name, name, location=None):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    custom_domains = get_custom_domains(cmd, resource_group_name, name, location)
    return custom_domains


def delete_hostname(cmd, resource_group_name, name, hostname, location=None):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    custom_domains = get_custom_domains(cmd, resource_group_name, name, location)
    new_custom_domains = list(filter(lambda c: c["name"] != hostname, custom_domains))
    if len(new_custom_domains) == len(custom_domains):
        raise ResourceNotFoundError("The hostname '{}' in Container app '{}' was not found.".format(hostname, name))

    r = patch_new_custom_domain(cmd, resource_group_name, name, new_custom_domains)
    logger.warning('Successfully deleted custom domain: {}'.format(hostname))
    return r


def show_storage(cmd, name, storage_name, resource_group_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        return StorageClient.show(cmd, resource_group_name, name, storage_name)
    except CLIError as e:
        handle_raw_exception(e)


def list_storage(cmd, name, resource_group_name):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        return StorageClient.list(cmd, resource_group_name, name)
    except CLIError as e:
        handle_raw_exception(e)


def create_or_update_storage(cmd, storage_name, resource_group_name, name, azure_file_account_name, azure_file_share_name, azure_file_account_key, access_mode, no_wait=False):  # pylint: disable=redefined-builtin
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    if len(azure_file_share_name) < 3:
        raise ValidationError("File share name must be longer than 2 characters.")

    if len(azure_file_account_name) < 3:
        raise ValidationError("Account name must be longer than 2 characters.")

    r = None

    try:
        r = StorageClient.show(cmd, resource_group_name, name, storage_name)
    except:
        pass

    if r:
        logger.warning("Only AzureFile account keys can be updated. In order to change the AzureFile share name or account name, please delete this storage and create a new one.")

    storage_def = AzureFilePropertiesModel
    storage_def["accountKey"] = azure_file_account_key
    storage_def["accountName"] = azure_file_account_name
    storage_def["shareName"] = azure_file_share_name
    storage_def["accessMode"] = access_mode
    storage_envelope = {}
    storage_envelope["properties"] = {}
    storage_envelope["properties"]["azureFile"] = storage_def

    try:
        return StorageClient.create_or_update(cmd, resource_group_name, name, storage_name, storage_envelope, no_wait)
    except CLIError as e:
        handle_raw_exception(e)


def remove_storage(cmd, storage_name, name, resource_group_name, no_wait=False):
    _validate_subscription_registered(cmd, CONTAINER_APPS_RP)

    try:
        return StorageClient.delete(cmd, resource_group_name, name, storage_name, no_wait)
    except CLIError as e:
        handle_raw_exception(e)


# TODO: Refactor provider code to make it cleaner
def update_aad_settings(cmd, resource_group_name, name,
                        client_id=None, client_secret_setting_name=None,
                        issuer=None, allowed_token_audiences=None, client_secret=None,
                        client_secret_certificate_thumbprint=None,
                        client_secret_certificate_san=None,
                        client_secret_certificate_issuer=None,
                        yes=False, tenant_id=None):

    try:
        show_ingress(cmd, name, resource_group_name)
    except Exception as e:
        raise ValidationError("Authentication requires ingress to be enabled for your containerapp.") from e

    if client_secret is not None and client_secret_setting_name is not None:
        raise ArgumentUsageError('Usage Error: --client-secret and --client-secret-setting-name cannot both be '
                                 'configured to non empty strings')

    if client_secret_setting_name is not None and client_secret_certificate_thumbprint is not None:
        raise ArgumentUsageError('Usage Error: --client-secret-setting-name and --thumbprint cannot both be '
                                 'configured to non empty strings')

    if client_secret is not None and client_secret_certificate_thumbprint is not None:
        raise ArgumentUsageError('Usage Error: --client-secret and --thumbprint cannot both be '
                                 'configured to non empty strings')

    if client_secret is not None and client_secret_certificate_san is not None:
        raise ArgumentUsageError('Usage Error: --client-secret and --san cannot both be '
                                 'configured to non empty strings')

    if client_secret_setting_name is not None and client_secret_certificate_san is not None:
        raise ArgumentUsageError('Usage Error: --client-secret-setting-name and --san cannot both be '
                                 'configured to non empty strings')

    if client_secret_certificate_thumbprint is not None and client_secret_certificate_san is not None:
        raise ArgumentUsageError('Usage Error: --thumbprint and --san cannot both be '
                                 'configured to non empty strings')

    if ((client_secret_certificate_san is not None and client_secret_certificate_issuer is None) or
            (client_secret_certificate_san is None and client_secret_certificate_issuer is not None)):
        raise ArgumentUsageError('Usage Error: --san and --certificate-issuer must both be '
                                 'configured to non empty strings')

    if issuer is not None and (tenant_id is not None):
        raise ArgumentUsageError('Usage Error: --issuer and --tenant-id cannot be configured '
                                 'to non empty strings at the same time.')

    is_new_aad_app = False
    existing_auth = {}
    try:
        existing_auth = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        existing_auth = {}
        existing_auth["platform"] = {}
        existing_auth["platform"]["enabled"] = True
        existing_auth["globalValidation"] = {}
        existing_auth["login"] = {}

    registration = {}
    validation = {}
    if "identityProviders" not in existing_auth:
        existing_auth["identityProviders"] = {}
    if "azureActiveDirectory" not in existing_auth["identityProviders"]:
        existing_auth["identityProviders"]["azureActiveDirectory"] = {}
        is_new_aad_app = True

    if is_new_aad_app and issuer is None and tenant_id is None:
        raise ArgumentUsageError('Usage Error: Either --issuer or --tenant-id must be specified when configuring the '
                                 'Microsoft auth registration.')

    if client_secret is not None and not yes:
        msg = 'Configuring --client-secret will add a secret to the containerapp. Are you sure you want to continue?'
        if not prompt_y_n(msg, default="n"):
            raise ArgumentUsageError('Usage Error: --client-secret cannot be used without agreeing to add secret '
                                     'to the containerapp.')

    openid_issuer = issuer
    if openid_issuer is None:
        # cmd.cli_ctx.cloud resolves to whichever cloud the customer is currently logged into
        authority = cmd.cli_ctx.cloud.endpoints.active_directory

        if tenant_id is not None:
            openid_issuer = authority + "/" + tenant_id + "/v2.0"

    registration = {}
    validation = {}
    if "identityProviders" not in existing_auth:
        existing_auth["identityProviders"] = {}
    if "azureActiveDirectory" not in existing_auth["identityProviders"]:
        existing_auth["identityProviders"]["azureActiveDirectory"] = {}
    if (client_id is not None or client_secret is not None or
            client_secret_setting_name is not None or openid_issuer is not None or
            client_secret_certificate_thumbprint is not None or
            client_secret_certificate_san is not None or
            client_secret_certificate_issuer is not None):
        if "registration" not in existing_auth["identityProviders"]["azureActiveDirectory"]:
            existing_auth["identityProviders"]["azureActiveDirectory"]["registration"] = {}
        registration = existing_auth["identityProviders"]["azureActiveDirectory"]["registration"]
    if allowed_token_audiences is not None:
        if "validation" not in existing_auth["identityProviders"]["azureActiveDirectory"]:
            existing_auth["identityProviders"]["azureActiveDirectory"]["validation"] = {}
        validation = existing_auth["identityProviders"]["azureActiveDirectory"]["validation"]

    if client_id is not None:
        registration["clientId"] = client_id
    if client_secret_setting_name is not None:
        registration["clientSecretSettingName"] = client_secret_setting_name
    if client_secret is not None:
        registration["clientSecretSettingName"] = MICROSOFT_SECRET_SETTING_NAME
        set_secrets(cmd, name, resource_group_name, secrets=[f"{MICROSOFT_SECRET_SETTING_NAME}={client_secret}"], no_wait=True, disable_max_length=True)
    if client_secret_setting_name is not None or client_secret is not None:
        fields = ["clientSecretCertificateThumbprint", "clientSecretCertificateSubjectAlternativeName", "clientSecretCertificateIssuer"]
        for field in [f for f in fields if registration.get(f)]:
            registration[field] = None
    if client_secret_certificate_thumbprint is not None:
        registration["clientSecretCertificateThumbprint"] = client_secret_certificate_thumbprint
        fields = ["clientSecretSettingName", "clientSecretCertificateSubjectAlternativeName", "clientSecretCertificateIssuer"]
        for field in [f for f in fields if registration.get(f)]:
            registration[field] = None
    if client_secret_certificate_san is not None:
        registration["clientSecretCertificateSubjectAlternativeName"] = client_secret_certificate_san
    if client_secret_certificate_issuer is not None:
        registration["clientSecretCertificateIssuer"] = client_secret_certificate_issuer
    if client_secret_certificate_san is not None and client_secret_certificate_issuer is not None:
        if "clientSecretSettingName" in registration:
            registration["clientSecretSettingName"] = None
        if "clientSecretCertificateThumbprint" in registration:
            registration["clientSecretCertificateThumbprint"] = None
    if openid_issuer is not None:
        registration["openIdIssuer"] = openid_issuer
    if allowed_token_audiences is not None:
        validation["allowedAudiences"] = allowed_token_audiences.split(",")
        existing_auth["identityProviders"]["azureActiveDirectory"]["validation"] = validation
    if (client_id is not None or client_secret is not None or
            client_secret_setting_name is not None or issuer is not None or
            client_secret_certificate_thumbprint is not None or
            client_secret_certificate_san is not None or
            client_secret_certificate_issuer is not None):
        existing_auth["identityProviders"]["azureActiveDirectory"]["registration"] = registration

    try:
        updated_auth_settings = AuthClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current", auth_config_envelope=existing_auth)["properties"]
        return updated_auth_settings["identityProviders"]["azureActiveDirectory"]
    except Exception as e:
        handle_raw_exception(e)


def get_aad_settings(cmd, resource_group_name, name):
    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        pass
    if "identityProviders" not in auth_settings:
        return {}
    if "azureActiveDirectory" not in auth_settings["identityProviders"]:
        return {}
    return auth_settings["identityProviders"]["azureActiveDirectory"]


def get_facebook_settings(cmd, resource_group_name, name):
    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        pass
    if "identityProviders" not in auth_settings:
        return {}
    if "facebook" not in auth_settings["identityProviders"]:
        return {}
    return auth_settings["identityProviders"]["facebook"]


def update_facebook_settings(cmd, resource_group_name, name,
                             app_id=None, app_secret_setting_name=None,
                             graph_api_version=None, scopes=None, app_secret=None, yes=False):
    try:
        show_ingress(cmd, name, resource_group_name)
    except Exception as e:
        raise ValidationError("Authentication requires ingress to be enabled for your containerapp.") from e

    if app_secret is not None and app_secret_setting_name is not None:
        raise ArgumentUsageError('Usage Error: --app-secret and --app-secret-setting-name cannot both be configured '
                                 'to non empty strings')

    if app_secret is not None and not yes:
        msg = 'Configuring --client-secret will add a secret to the containerapp. Are you sure you want to continue?'
        if not prompt_y_n(msg, default="n"):
            raise ArgumentUsageError('Usage Error: --client-secret cannot be used without agreeing to add secret '
                                     'to the containerapp.')

    existing_auth = {}
    try:
        existing_auth = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        existing_auth = {}
        existing_auth["platform"] = {}
        existing_auth["platform"]["enabled"] = True
        existing_auth["globalValidation"] = {}
        existing_auth["login"] = {}

    registration = {}
    if "identityProviders" not in existing_auth:
        existing_auth["identityProviders"] = {}
    if "facebook" not in existing_auth["identityProviders"]:
        existing_auth["identityProviders"]["facebook"] = {}
    if app_id is not None or app_secret is not None or app_secret_setting_name is not None:
        if "registration" not in existing_auth["identityProviders"]["facebook"]:
            existing_auth["identityProviders"]["facebook"]["registration"] = {}
        registration = existing_auth["identityProviders"]["facebook"]["registration"]
    if scopes is not None:
        if "login" not in existing_auth["identityProviders"]["facebook"]:
            existing_auth["identityProviders"]["facebook"]["login"] = {}

    if app_id is not None:
        registration["appId"] = app_id
    if app_secret_setting_name is not None:
        registration["appSecretSettingName"] = app_secret_setting_name
    if app_secret is not None:
        registration["appSecretSettingName"] = FACEBOOK_SECRET_SETTING_NAME
        set_secrets(cmd, name, resource_group_name, secrets=[f"{FACEBOOK_SECRET_SETTING_NAME}={app_secret}"], no_wait=True, disable_max_length=True)
    if graph_api_version is not None:
        existing_auth["identityProviders"]["facebook"]["graphApiVersion"] = graph_api_version
    if scopes is not None:
        existing_auth["identityProviders"]["facebook"]["login"]["scopes"] = scopes.split(",")
    if app_id is not None or app_secret is not None or app_secret_setting_name is not None:
        existing_auth["identityProviders"]["facebook"]["registration"] = registration

    try:
        updated_auth_settings = AuthClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current", auth_config_envelope=existing_auth)["properties"]
        return updated_auth_settings["identityProviders"]["facebook"]
    except Exception as e:
        handle_raw_exception(e)


def get_github_settings(cmd, resource_group_name, name):
    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        pass
    if "identityProviders" not in auth_settings:
        return {}
    if "gitHub" not in auth_settings["identityProviders"]:
        return {}
    return auth_settings["identityProviders"]["gitHub"]


def update_github_settings(cmd, resource_group_name, name,
                           client_id=None, client_secret_setting_name=None,
                           scopes=None, client_secret=None, yes=False):
    try:
        show_ingress(cmd, name, resource_group_name)
    except Exception as e:
        raise ValidationError("Authentication requires ingress to be enabled for your containerapp.") from e

    if client_secret is not None and client_secret_setting_name is not None:
        raise ArgumentUsageError('Usage Error: --client-secret and --client-secret-setting-name cannot '
                                 'both be configured to non empty strings')

    if client_secret is not None and not yes:
        msg = 'Configuring --client-secret will add a secret to the containerapp. Are you sure you want to continue?'
        if not prompt_y_n(msg, default="n"):
            raise ArgumentUsageError('Usage Error: --client-secret cannot be used without agreeing to add secret '
                                     'to the containerapp.')

    existing_auth = {}
    try:
        existing_auth = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        existing_auth = {}
        existing_auth["platform"] = {}
        existing_auth["platform"]["enabled"] = True
        existing_auth["globalValidation"] = {}
        existing_auth["login"] = {}

    registration = {}
    if "identityProviders" not in existing_auth:
        existing_auth["identityProviders"] = {}
    if "gitHub" not in existing_auth["identityProviders"]:
        existing_auth["identityProviders"]["gitHub"] = {}
    if client_id is not None or client_secret is not None or client_secret_setting_name is not None:
        if "registration" not in existing_auth["identityProviders"]["gitHub"]:
            existing_auth["identityProviders"]["gitHub"]["registration"] = {}
        registration = existing_auth["identityProviders"]["gitHub"]["registration"]
    if scopes is not None:
        if "login" not in existing_auth["identityProviders"]["gitHub"]:
            existing_auth["identityProviders"]["gitHub"]["login"] = {}

    if client_id is not None:
        registration["clientId"] = client_id
    if client_secret_setting_name is not None:
        registration["clientSecretSettingName"] = client_secret_setting_name
    if client_secret is not None:
        registration["clientSecretSettingName"] = GITHUB_SECRET_SETTING_NAME
        set_secrets(cmd, name, resource_group_name, secrets=[f"{GITHUB_SECRET_SETTING_NAME}={client_secret}"], no_wait=True, disable_max_length=True)
    if scopes is not None:
        existing_auth["identityProviders"]["gitHub"]["login"]["scopes"] = scopes.split(",")
    if client_id is not None or client_secret is not None or client_secret_setting_name is not None:
        existing_auth["identityProviders"]["gitHub"]["registration"] = registration

    try:
        updated_auth_settings = AuthClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current", auth_config_envelope=existing_auth)["properties"]
        return updated_auth_settings["identityProviders"]["gitHub"]
    except Exception as e:
        handle_raw_exception(e)


def get_google_settings(cmd, resource_group_name, name):
    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        pass
    if "identityProviders" not in auth_settings:
        return {}
    if "google" not in auth_settings["identityProviders"]:
        return {}
    return auth_settings["identityProviders"]["google"]


def update_google_settings(cmd, resource_group_name, name,
                           client_id=None, client_secret_setting_name=None,
                           scopes=None, allowed_token_audiences=None, client_secret=None, yes=False):
    try:
        show_ingress(cmd, name, resource_group_name)
    except Exception as e:
        raise ValidationError("Authentication requires ingress to be enabled for your containerapp.") from e

    if client_secret is not None and client_secret_setting_name is not None:
        raise ArgumentUsageError('Usage Error: --client-secret and --client-secret-setting-name cannot '
                                 'both be configured to non empty strings')

    if client_secret is not None and not yes:
        msg = 'Configuring --client-secret will add a secret to the containerapp. Are you sure you want to continue?'
        if not prompt_y_n(msg, default="n"):
            raise ArgumentUsageError('Usage Error: --client-secret cannot be used without agreeing to add secret '
                                     'to the containerapp.')

    existing_auth = {}
    try:
        existing_auth = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        existing_auth = {}
        existing_auth["platform"] = {}
        existing_auth["platform"]["enabled"] = True
        existing_auth["globalValidation"] = {}
        existing_auth["login"] = {}

    registration = {}
    validation = {}
    if "identityProviders" not in existing_auth:
        existing_auth["identityProviders"] = {}
    if "google" not in existing_auth["identityProviders"]:
        existing_auth["identityProviders"]["google"] = {}
    if client_id is not None or client_secret is not None or client_secret_setting_name is not None:
        if "registration" not in existing_auth["identityProviders"]["google"]:
            existing_auth["identityProviders"]["google"]["registration"] = {}
        registration = existing_auth["identityProviders"]["google"]["registration"]
    if scopes is not None:
        if "login" not in existing_auth["identityProviders"]["google"]:
            existing_auth["identityProviders"]["google"]["login"] = {}
    if allowed_token_audiences is not None:
        if "validation" not in existing_auth["identityProviders"]["google"]:
            existing_auth["identityProviders"]["google"]["validation"] = {}

    if client_id is not None:
        registration["clientId"] = client_id
    if client_secret_setting_name is not None:
        registration["clientSecretSettingName"] = client_secret_setting_name
    if client_secret is not None:
        registration["clientSecretSettingName"] = GOOGLE_SECRET_SETTING_NAME
        set_secrets(cmd, name, resource_group_name, secrets=[f"{GOOGLE_SECRET_SETTING_NAME}={client_secret}"], no_wait=True, disable_max_length=True)
    if scopes is not None:
        existing_auth["identityProviders"]["google"]["login"]["scopes"] = scopes.split(",")
    if allowed_token_audiences is not None:
        validation["allowedAudiences"] = allowed_token_audiences.split(",")
        existing_auth["identityProviders"]["google"]["validation"] = validation
    if client_id is not None or client_secret is not None or client_secret_setting_name is not None:
        existing_auth["identityProviders"]["google"]["registration"] = registration

    try:
        updated_auth_settings = AuthClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current", auth_config_envelope=existing_auth)["properties"]
        return updated_auth_settings["identityProviders"]["google"]
    except Exception as e:
        handle_raw_exception(e)


def get_twitter_settings(cmd, resource_group_name, name):
    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        pass
    if "identityProviders" not in auth_settings:
        return {}
    if "twitter" not in auth_settings["identityProviders"]:
        return {}
    return auth_settings["identityProviders"]["twitter"]


def update_twitter_settings(cmd, resource_group_name, name,
                            consumer_key=None, consumer_secret_setting_name=None,
                            consumer_secret=None, yes=False):
    try:
        show_ingress(cmd, name, resource_group_name)
    except Exception as e:
        raise ValidationError("Authentication requires ingress to be enabled for your containerapp.") from e

    if consumer_secret is not None and consumer_secret_setting_name is not None:
        raise ArgumentUsageError('Usage Error: --consumer-secret and --consumer-secret-setting-name cannot '
                                 'both be configured to non empty strings')

    if consumer_secret is not None and not yes:
        msg = 'Configuring --client-secret will add a secret to the containerapp. Are you sure you want to continue?'
        if not prompt_y_n(msg, default="n"):
            raise ArgumentUsageError('Usage Error: --client-secret cannot be used without agreeing to add secret '
                                     'to the containerapp.')

    existing_auth = {}
    try:
        existing_auth = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        existing_auth = {}
        existing_auth["platform"] = {}
        existing_auth["platform"]["enabled"] = True
        existing_auth["globalValidation"] = {}
        existing_auth["login"] = {}

    registration = {}
    if "identityProviders" not in existing_auth:
        existing_auth["identityProviders"] = {}
    if "twitter" not in existing_auth["identityProviders"]:
        existing_auth["identityProviders"]["twitter"] = {}
    if consumer_key is not None or consumer_secret is not None or consumer_secret_setting_name is not None:
        if "registration" not in existing_auth["identityProviders"]["twitter"]:
            existing_auth["identityProviders"]["twitter"]["registration"] = {}
        registration = existing_auth["identityProviders"]["twitter"]["registration"]

    if consumer_key is not None:
        registration["consumerKey"] = consumer_key
    if consumer_secret_setting_name is not None:
        registration["consumerSecretSettingName"] = consumer_secret_setting_name
    if consumer_secret is not None:
        registration["consumerSecretSettingName"] = TWITTER_SECRET_SETTING_NAME
        set_secrets(cmd, name, resource_group_name, secrets=[f"{TWITTER_SECRET_SETTING_NAME}={consumer_secret}"], no_wait=True, disable_max_length=True)
    if consumer_key is not None or consumer_secret is not None or consumer_secret_setting_name is not None:
        existing_auth["identityProviders"]["twitter"]["registration"] = registration
    try:
        updated_auth_settings = AuthClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current", auth_config_envelope=existing_auth)["properties"]
        return updated_auth_settings["identityProviders"]["twitter"]
    except Exception as e:
        handle_raw_exception(e)


def get_apple_settings(cmd, resource_group_name, name):
    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        pass
    if "identityProviders" not in auth_settings:
        return {}
    if "apple" not in auth_settings["identityProviders"]:
        return {}
    return auth_settings["identityProviders"]["apple"]


def update_apple_settings(cmd, resource_group_name, name,
                          client_id=None, client_secret_setting_name=None,
                          scopes=None, client_secret=None, yes=False):
    try:
        show_ingress(cmd, name, resource_group_name)
    except Exception as e:
        raise ValidationError("Authentication requires ingress to be enabled for your containerapp.") from e

    if client_secret is not None and client_secret_setting_name is not None:
        raise ArgumentUsageError('Usage Error: --client-secret and --client-secret-setting-name '
                                 'cannot both be configured to non empty strings')

    if client_secret is not None and not yes:
        msg = 'Configuring --client-secret will add a secret to the containerapp. Are you sure you want to continue?'
        if not prompt_y_n(msg, default="n"):
            raise ArgumentUsageError('Usage Error: --client-secret cannot be used without agreeing to add secret '
                                     'to the containerapp.')

    existing_auth = {}
    try:
        existing_auth = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        existing_auth = {}
        existing_auth["platform"] = {}
        existing_auth["platform"]["enabled"] = True
        existing_auth["globalValidation"] = {}
        existing_auth["login"] = {}

    registration = {}
    if "identityProviders" not in existing_auth:
        existing_auth["identityProviders"] = {}
    if "apple" not in existing_auth["identityProviders"]:
        existing_auth["identityProviders"]["apple"] = {}
    if client_id is not None or client_secret is not None or client_secret_setting_name is not None:
        if "registration" not in existing_auth["identityProviders"]["apple"]:
            existing_auth["identityProviders"]["apple"]["registration"] = {}
        registration = existing_auth["identityProviders"]["apple"]["registration"]
    if scopes is not None:
        if "login" not in existing_auth["identityProviders"]["apple"]:
            existing_auth["identityProviders"]["apple"]["login"] = {}

    if client_id is not None:
        registration["clientId"] = client_id
    if client_secret_setting_name is not None:
        registration["clientSecretSettingName"] = client_secret_setting_name
    if client_secret is not None:
        registration["clientSecretSettingName"] = APPLE_SECRET_SETTING_NAME
        set_secrets(cmd, name, resource_group_name, secrets=[f"{APPLE_SECRET_SETTING_NAME}={client_secret}"], no_wait=True, disable_max_length=True)
    if scopes is not None:
        existing_auth["identityProviders"]["apple"]["login"]["scopes"] = scopes.split(",")
    if client_id is not None or client_secret is not None or client_secret_setting_name is not None:
        existing_auth["identityProviders"]["apple"]["registration"] = registration

    try:
        updated_auth_settings = AuthClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current", auth_config_envelope=existing_auth)["properties"]
        return updated_auth_settings["identityProviders"]["apple"]
    except Exception as e:
        handle_raw_exception(e)


def get_openid_connect_provider_settings(cmd, resource_group_name, name, provider_name):
    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        pass
    if "identityProviders" not in auth_settings:
        raise ArgumentUsageError('Usage Error: The following custom OpenID Connect provider '
                                 'has not been configured: ' + provider_name)
    if "customOpenIdConnectProviders" not in auth_settings["identityProviders"]:
        raise ArgumentUsageError('Usage Error: The following custom OpenID Connect provider '
                                 'has not been configured: ' + provider_name)
    if provider_name not in auth_settings["identityProviders"]["customOpenIdConnectProviders"]:
        raise ArgumentUsageError('Usage Error: The following custom OpenID Connect provider '
                                 'has not been configured: ' + provider_name)
    return auth_settings["identityProviders"]["customOpenIdConnectProviders"][provider_name]


def add_openid_connect_provider_settings(cmd, resource_group_name, name, provider_name,
                                         client_id=None, client_secret_setting_name=None,
                                         openid_configuration=None, scopes=None,
                                         client_secret=None, yes=False):
    from ._utils import get_oidc_client_setting_app_setting_name
    try:
        show_ingress(cmd, name, resource_group_name)
    except Exception as e:
        raise ValidationError("Authentication requires ingress to be enabled for your containerapp.") from e

    if client_secret is not None and not yes:
        msg = 'Configuring --client-secret will add a secret to the containerapp. Are you sure you want to continue?'
        if not prompt_y_n(msg, default="n"):
            raise ArgumentUsageError('Usage Error: --client-secret cannot be used without agreeing to add secret '
                                     'to the containerapp.')

    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        auth_settings = {}
        auth_settings["platform"] = {}
        auth_settings["platform"]["enabled"] = True
        auth_settings["globalValidation"] = {}
        auth_settings["login"] = {}

    if "identityProviders" not in auth_settings:
        auth_settings["identityProviders"] = {}
    if "customOpenIdConnectProviders" not in auth_settings["identityProviders"]:
        auth_settings["identityProviders"]["customOpenIdConnectProviders"] = {}
    if provider_name in auth_settings["identityProviders"]["customOpenIdConnectProviders"]:
        raise ArgumentUsageError('Usage Error: The following custom OpenID Connect provider has already been '
                                 'configured: ' + provider_name + '. Please use `az containerapp auth oidc update` to '
                                 'update the provider.')

    final_client_secret_setting_name = client_secret_setting_name
    if client_secret is not None:
        final_client_secret_setting_name = get_oidc_client_setting_app_setting_name(provider_name)
        set_secrets(cmd, name, resource_group_name, secrets=[f"{final_client_secret_setting_name}={client_secret}"], no_wait=True, disable_max_length=True)

    auth_settings["identityProviders"]["customOpenIdConnectProviders"][provider_name] = {
        "registration": {
            "clientId": client_id,
            "clientCredential": {
                "clientSecretSettingName": final_client_secret_setting_name
            },
            "openIdConnectConfiguration": {
                "wellKnownOpenIdConfiguration": openid_configuration
            }
        }
    }
    login = {}
    if scopes is not None:
        login["scopes"] = scopes.split(',')
    else:
        login["scopes"] = ["openid"]

    auth_settings["identityProviders"]["customOpenIdConnectProviders"][provider_name]["login"] = login

    try:
        updated_auth_settings = AuthClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current", auth_config_envelope=auth_settings)["properties"]
        return updated_auth_settings["identityProviders"]["customOpenIdConnectProviders"][provider_name]
    except Exception as e:
        handle_raw_exception(e)


def update_openid_connect_provider_settings(cmd, resource_group_name, name, provider_name,
                                            client_id=None, client_secret_setting_name=None,
                                            openid_configuration=None, scopes=None,
                                            client_secret=None, yes=False):
    from ._utils import get_oidc_client_setting_app_setting_name
    try:
        show_ingress(cmd, name, resource_group_name)
    except Exception as e:
        raise ValidationError("Authentication requires ingress to be enabled for your containerapp.") from e

    if client_secret is not None and not yes:
        msg = 'Configuring --client-secret will add a secret to the containerapp. Are you sure you want to continue?'
        if not prompt_y_n(msg, default="n"):
            raise ArgumentUsageError('Usage Error: --client-secret cannot be used without agreeing to add secret '
                                     'to the containerapp.')

    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        auth_settings = {}
        auth_settings["platform"] = {}
        auth_settings["platform"]["enabled"] = True
        auth_settings["globalValidation"] = {}
        auth_settings["login"] = {}

    if "identityProviders" not in auth_settings:
        raise ArgumentUsageError('Usage Error: The following custom OpenID Connect provider '
                                 'has not been configured: ' + provider_name)
    if "customOpenIdConnectProviders" not in auth_settings["identityProviders"]:
        raise ArgumentUsageError('Usage Error: The following custom OpenID Connect provider '
                                 'has not been configured: ' + provider_name)
    if provider_name not in auth_settings["identityProviders"]["customOpenIdConnectProviders"]:
        raise ArgumentUsageError('Usage Error: The following custom OpenID Connect provider '
                                 'has not been configured: ' + provider_name)

    custom_open_id_connect_providers = auth_settings["identityProviders"]["customOpenIdConnectProviders"]
    registration = {}
    if client_id is not None or client_secret_setting_name is not None or openid_configuration is not None:
        if "registration" not in custom_open_id_connect_providers[provider_name]:
            custom_open_id_connect_providers[provider_name]["registration"] = {}
        registration = custom_open_id_connect_providers[provider_name]["registration"]

    if client_secret_setting_name is not None or client_secret is not None:
        if "clientCredential" not in custom_open_id_connect_providers[provider_name]["registration"]:
            custom_open_id_connect_providers[provider_name]["registration"]["clientCredential"] = {}

    if openid_configuration is not None:
        if "openIdConnectConfiguration" not in custom_open_id_connect_providers[provider_name]["registration"]:
            custom_open_id_connect_providers[provider_name]["registration"]["openIdConnectConfiguration"] = {}

    if scopes is not None:
        if "login" not in auth_settings["identityProviders"]["customOpenIdConnectProviders"][provider_name]:
            custom_open_id_connect_providers[provider_name]["login"] = {}

    if client_id is not None:
        registration["clientId"] = client_id
    if client_secret_setting_name is not None:
        registration["clientCredential"]["clientSecretSettingName"] = client_secret_setting_name
    if client_secret is not None:
        final_client_secret_setting_name = get_oidc_client_setting_app_setting_name(provider_name)
        registration["clientSecretSettingName"] = final_client_secret_setting_name
        set_secrets(cmd, name, resource_group_name, secrets=[f"{final_client_secret_setting_name}={client_secret}"], no_wait=True, disable_max_length=True)
    if openid_configuration is not None:
        registration["openIdConnectConfiguration"]["wellKnownOpenIdConfiguration"] = openid_configuration
    if scopes is not None:
        custom_open_id_connect_providers[provider_name]["login"]["scopes"] = scopes.split(",")
    if client_id is not None or client_secret_setting_name is not None or openid_configuration is not None:
        custom_open_id_connect_providers[provider_name]["registration"] = registration
    auth_settings["identityProviders"]["customOpenIdConnectProviders"] = custom_open_id_connect_providers

    try:
        updated_auth_settings = AuthClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current", auth_config_envelope=auth_settings)["properties"]
        return updated_auth_settings["identityProviders"]["customOpenIdConnectProviders"][provider_name]
    except Exception as e:
        handle_raw_exception(e)


def remove_openid_connect_provider_settings(cmd, resource_group_name, name, provider_name):
    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        pass
    if "identityProviders" not in auth_settings:
        raise ArgumentUsageError('Usage Error: The following custom OpenID Connect provider '
                                 'has not been configured: ' + provider_name)
    if "customOpenIdConnectProviders" not in auth_settings["identityProviders"]:
        raise ArgumentUsageError('Usage Error: The following custom OpenID Connect provider '
                                 'has not been configured: ' + provider_name)
    if provider_name not in auth_settings["identityProviders"]["customOpenIdConnectProviders"]:
        raise ArgumentUsageError('Usage Error: The following custom OpenID Connect provider '
                                 'has not been configured: ' + provider_name)
    auth_settings["identityProviders"]["customOpenIdConnectProviders"].pop(provider_name, None)
    try:
        AuthClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current", auth_config_envelope=auth_settings)
        return {}
    except Exception as e:
        handle_raw_exception(e)


def update_auth_config(cmd, resource_group_name, name, set_string=None, enabled=None,
                       runtime_version=None, config_file_path=None, unauthenticated_client_action=None,
                       redirect_provider=None, enable_token_store=None, require_https=None,
                       proxy_convention=None, proxy_custom_host_header=None,
                       proxy_custom_proto_header=None, excluded_paths=None):
    from ._utils import set_field_in_auth_settings, update_http_settings_in_auth_settings
    existing_auth = {}
    try:
        existing_auth = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        existing_auth["platform"] = {}
        existing_auth["platform"]["enabled"] = True
        existing_auth["globalValidation"] = {}
        existing_auth["login"] = {}

    existing_auth = set_field_in_auth_settings(existing_auth, set_string)

    if enabled is not None:
        if "platform" not in existing_auth:
            existing_auth["platform"] = {}
        existing_auth["platform"]["enabled"] = enabled

    if runtime_version is not None:
        if "platform" not in existing_auth:
            existing_auth["platform"] = {}
        existing_auth["platform"]["runtimeVersion"] = runtime_version

    if config_file_path is not None:
        if "platform" not in existing_auth:
            existing_auth["platform"] = {}
        existing_auth["platform"]["configFilePath"] = config_file_path

    if unauthenticated_client_action is not None:
        if "globalValidation" not in existing_auth:
            existing_auth["globalValidation"] = {}
        existing_auth["globalValidation"]["unauthenticatedClientAction"] = unauthenticated_client_action

    if redirect_provider is not None:
        if "globalValidation" not in existing_auth:
            existing_auth["globalValidation"] = {}
        existing_auth["globalValidation"]["redirectToProvider"] = redirect_provider

    if enable_token_store is not None:
        if "login" not in existing_auth:
            existing_auth["login"] = {}
        if "tokenStore" not in existing_auth["login"]:
            existing_auth["login"]["tokenStore"] = {}
        existing_auth["login"]["tokenStore"]["enabled"] = enable_token_store

    if excluded_paths is not None:
        if "globalValidation" not in existing_auth:
            existing_auth["globalValidation"] = {}
        excluded_paths_list_string = excluded_paths[1:-1]
        existing_auth["globalValidation"]["excludedPaths"] = excluded_paths_list_string.split(",")

    existing_auth = update_http_settings_in_auth_settings(existing_auth, require_https,
                                                          proxy_convention, proxy_custom_host_header,
                                                          proxy_custom_proto_header)
    try:
        return AuthClient.create_or_update(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current", auth_config_envelope=existing_auth)
    except Exception as e:
        handle_raw_exception(e)


def show_auth_config(cmd, resource_group_name, name):
    auth_settings = {}
    try:
        auth_settings = AuthClient.get(cmd=cmd, resource_group_name=resource_group_name, container_app_name=name, auth_config_name="current")["properties"]
    except:
        pass
    return auth_settings

# Compose


def create_containerapps_from_compose(cmd,  # pylint: disable=R0914
                                      resource_group_name,
                                      managed_env,
                                      compose_file_path='./docker-compose.yml',
                                      registry_server=None,
                                      registry_user=None,
                                      registry_pass=None,
                                      transport_mapping=None,
                                      location=None,
                                      tags=None):

    from pycomposefile import ComposeFile

    from ._compose_utils import (create_containerapps_compose_environment,
                                 build_containerapp_from_compose_service,
                                 check_supported_platform,
                                 warn_about_unsupported_elements,
                                 resolve_ingress_and_target_port,
                                 resolve_registry_from_cli_args,
                                 resolve_transport_from_cli_args,
                                 resolve_service_startup_command,
                                 validate_memory_and_cpu_setting,
                                 resolve_cpu_configuration_from_service,
                                 resolve_memory_configuration_from_service,
                                 resolve_replicas_from_service,
                                 resolve_environment_from_service,
                                 resolve_secret_from_service)

    # Validate managed environment
    parsed_managed_env = parse_resource_id(managed_env)
    managed_env_name = parsed_managed_env['name']

    logger.info(   # pylint: disable=W1203
        f"Creating the Container Apps managed environment {managed_env_name} under {resource_group_name} in {location}.")

    try:
        managed_environment = show_managed_environment(cmd=cmd,
                                                       name=managed_env_name,
                                                       resource_group_name=resource_group_name)
    except CLIInternalError:  # pylint: disable=W0702
        managed_environment = create_containerapps_compose_environment(cmd,
                                                                       managed_env_name,
                                                                       resource_group_name,
                                                                       tags=tags)

    compose_yaml = load_yaml_file(compose_file_path)
    parsed_compose_file = ComposeFile(compose_yaml)
    logger.info(parsed_compose_file)
    containerapps_from_compose = []
    # Using the key to iterate to get the service name
    # pylint: disable=C0201,C0206
    for service_name in parsed_compose_file.ordered_services.keys():
        service = parsed_compose_file.services[service_name]
        if not check_supported_platform(service.platform):
            message = "Unsupported platform found. "
            message += "Azure Container Apps only supports linux/amd64 container images."
            raise InvalidArgumentValueError(message)
        image = service.image
        warn_about_unsupported_elements(service)
        logger.info(  # pylint: disable=W1203
            f"Creating the Container Apps instance for {service_name} under {resource_group_name} in {location}.")
        ingress_type, target_port = resolve_ingress_and_target_port(service)
        registry, registry_username, registry_password = resolve_registry_from_cli_args(registry_server, registry_user, registry_pass)  # pylint: disable=C0301
        transport_setting = resolve_transport_from_cli_args(service_name, transport_mapping)
        startup_command, startup_args = resolve_service_startup_command(service)
        cpu, memory = validate_memory_and_cpu_setting(
            resolve_cpu_configuration_from_service(service),
            resolve_memory_configuration_from_service(service)
        )
        replicas = resolve_replicas_from_service(service)
        environment = resolve_environment_from_service(service)
        secret_vars, secret_env_ref = resolve_secret_from_service(service, parsed_compose_file.secrets)
        if environment is not None and secret_env_ref is not None:
            environment.extend(secret_env_ref)
        elif secret_env_ref is not None:
            environment = secret_env_ref
        if service.build is not None:
            logger.warning("Build configuration defined for this service.")
            logger.warning("The build will be performed by Azure Container Registry.")
            context = service.build.context
            dockerfile = "Dockerfile"
            if service.build.dockerfile is not None:
                dockerfile = service.build.dockerfile
            image, registry, registry_username, registry_password = build_containerapp_from_compose_service(
                cmd,
                service_name,
                context,
                dockerfile,
                resource_group_name,
                managed_env,
                location,
                image,
                target_port,
                ingress_type,
                registry,
                registry_username,
                registry_password,
                environment)
        containerapps_from_compose.append(
            create_containerapp(cmd,
                                service_name,
                                resource_group_name,
                                image=image,
                                container_name=service.container_name,
                                managed_env=managed_environment["id"],
                                ingress=ingress_type,
                                target_port=target_port,
                                registry_server=registry,
                                registry_user=registry_username,
                                registry_pass=registry_password,
                                transport=transport_setting,
                                startup_command=startup_command,
                                args=startup_args,
                                cpu=cpu,
                                memory=memory,
                                env_vars=environment,
                                secrets=secret_vars,
                                min_replicas=replicas,
                                max_replicas=replicas,)
        )
    return containerapps_from_compose
