from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Union

import boto3
import questionary
from botocore.exceptions import ClientError

from iambic.aws.cloud_formation.utils import create_iambic_stacks
from iambic.aws.iam.policy.models import PolicyDocument, PolicyStatement
from iambic.aws.iam.role.models import AWS_IAM_ROLE_TEMPLATE_TYPE, RoleTemplate
from iambic.aws.iam.role.template_generation import generate_aws_role_templates
from iambic.aws.models import (
    AWSAccount,
    AWSIdentityCenterAccount,
    AWSOrganization,
    BaseAWSOrgRule,
    Partition,
)
from iambic.aws.utils import boto_crud_call
from iambic.config.models import (
    CURRENT_IAMBIC_VERSION,
    Config,
    ExtendsConfig,
    ExtendsConfigKey,
    GoogleProject,
    OktaOrganization,
)
from iambic.config.utils import resolve_config_template_path
from iambic.core.context import ctx
from iambic.core.iambic_enum import IambicManaged
from iambic.core.logger import log
from iambic.core.parser import load_templates
from iambic.core.template_generation import get_existing_template_map
from iambic.core.utils import yaml
from iambic.github.utils import create_workflow_files

IAMBIC_SPOKE_ROLE_TEMPLATE_FP = os.path.join(
    os.path.dirname(__file__), "../aws/iam/role/templates/iambicspokerole.yaml"
)
SPOKE_ROLE_TEMPLATE = load_templates([IAMBIC_SPOKE_ROLE_TEMPLATE_FP])[0]


async def get_org_account_id(aws_org: AWSOrganization) -> str:
    client = await aws_org.get_boto3_client("organizations")
    response = await boto_crud_call(
        client.describe_organization,
    )
    return response.get("MasterAccountId")


def set_iambic_control(default_val: Union[str, IambicManaged]) -> str:
    if isinstance(default_val, IambicManaged):
        default_val = default_val.value

    if default_val == IambicManaged.UNDEFINED.value:
        params = {}
    else:
        params = {"default": default_val}

    return questionary.select(
        "How much control should Iambic have?",
        choices=[e.value for e in IambicManaged if e != IambicManaged.UNDEFINED],
        **params,
    ).ask()


def set_aws_account_partition(default_val: Union[str, Partition]) -> str:
    return questionary.select(
        "Which AWS partition is the account on?",
        choices=[e.value for e in Partition],
        default=default_val if isinstance(default_val, str) else default_val.value,
    ).ask()


def set_aws_org_assume_role_name(default_val: Union[list, str]) -> Union[list, str]:
    if isinstance(default_val, list):
        default_val = ", ".join(default_val)
    return sorted(
        questionary.text(
            "What are the role name(s) the org should attempt to use to assume into the spoke accounts? (comma separated)",
            default=default_val,
        )
        .ask()
        .replace(" ", "")
        .split(",")
    )


def set_required_text_value(human_readable_name: str, default_val: str = None):
    while True:
        if response := questionary.text(
            f"What is the {human_readable_name}?",
            default=default_val or "",
        ).ask():
            return response
        else:
            print(f"Please enter a valid {human_readable_name}.")


def set_okta_idp_name(default_val: str = None):
    return set_required_text_value("Okta Identity Provider Name", default_val)


def set_okta_org_url(default_val: str = None):
    return set_required_text_value("Okta Organization URL", default_val)


def set_okta_api_token(default_val: str = None):
    return set_required_text_value("Okta API Token", default_val)


def set_google_subject(default_domain: str = None, default_service: str = None) -> dict:
    return {
        "domain": set_required_text_value("Google Domain", default_domain),
        "service_account": set_required_text_value(
            "Google Service Account", default_service
        ),
    }


def set_google_project_type(default_val: str = None):
    return set_required_text_value(
        "Google Project Type", default_val or "service_account"
    )


def set_google_project_id(default_val: str = None):
    return set_required_text_value("Project ID", default_val)


def set_google_private_key(default_val: str = None):
    return set_required_text_value("Private Key", default_val)


def set_google_private_key_id(default_val: str = None):
    return set_required_text_value("Private Key ID", default_val)


def set_google_client_id(default_val: str = None):
    return set_required_text_value("Client ID", default_val)


def set_google_client_email(default_val: str = None):
    return set_required_text_value("Client E-Mail", default_val)


def set_google_auth_uri(default_val: str = None):
    return set_required_text_value("Auth URI", default_val)


def set_google_token_uri(default_val: str = None):
    return set_required_text_value("Token URI", default_val)


def set_google_auth_provider_cert_url(default_val: str = None):
    return set_required_text_value("auth_provider_x509_cert_url", default_val)


def set_google_client_cert_url(default_val: str = None):
    return set_required_text_value("client_x509_cert_url", default_val)


def set_identity_center_account(
    account_id: str, region: str = None
) -> AWSIdentityCenterAccount:
    region_params = {"default": region} if region else {}

    region = questionary.text(
        "What region is your Identity Center (SSO) set to? Example: `us-east-1`",
        **region_params,
    ).ask()

    identity_center_account = AWSIdentityCenterAccount(
        account_id=account_id, region=region
    )
    return identity_center_account


class ConfigurationWizard:
    def __init__(self, repo_dir: str):
        # TODO: Handle the case where the config file exists but is not valid
        session = boto3.Session()
        self.autodetected_org_settings = {}
        self.existing_role_template_map = {}
        self.aws_account_map = {}
        self.default_region = session.region_name or "us-east-1"
        self.repo_dir = repo_dir

        try:
            self.config_path = asyncio.run(resolve_config_template_path(repo_dir))
        except RuntimeError:
            self.config_path = f"{repo_dir}/iambic_config.yaml"
        self.config: Config = Config(version=CURRENT_IAMBIC_VERSION)

        if os.path.exists(self.config_path) and os.path.getsize(self.config_path) != 0:
            log.info("Found existing configuration file", config_path=self.config_path)
            with contextlib.suppress(FileNotFoundError):
                # Try to load a configuration
                self.config = Config.load(self.config_path)
        with contextlib.suppress(ClientError):
            self.autodetected_org_settings = session.client(
                "organizations"
            ).describe_organization()["Organization"]

        asyncio.run(self.attempt_aws_account_refresh())

        log.debug("Starting configuration wizard", config_path=self.config_path)

    async def attempt_aws_account_refresh(self):
        self.aws_account_map = {}

        if not self.config.aws:
            return

        try:
            await self.config.setup_aws_accounts()
            for account in self.config.aws.accounts:
                if account.identity_center_details:
                    await account.set_identity_center_details()
        except Exception as err:
            log.debug("Failed to refresh AWS accounts", error=err)

        self.aws_account_map = {
            account.account_id: account for account in self.config.aws.accounts
        }

    def configuration_wizard_aws_account_add(self):
        account_id = questionary.text(
            "What is the AWS Account ID? Usually this looks like `12345689012`"
        ).ask()

        account_name = questionary.text("What is the name of the AWS Account?").ask()

        account = AWSAccount(
            account_id=account_id,
            account_name=account_name,
        )

        if assume_role_arn := questionary.text(
            "(Optional) Provide a role ARN to assume when accessing the AWS Organization"
            " from the current role. (If you are using a role with access to the Organization, "
            "you can leave this blank)"
        ).ask():
            account.assume_role_arn = assume_role_arn

        if aws_profile := questionary.text(
            "(Optional) Provide an AWS profile name (Configured in ~/.aws/config) that Iambic "
            "should use when we access the AWS Organization. Note: "
            "this profile will be used before assuming any subsequent roles. "
            "(If you are using a role with access to the Organization and don't use "
            "a profile, you can leave this blank)",
            default=os.environ.get("AWS_PROFILE"),
        ).ask():
            account.aws_profile = aws_profile

        account.iambic_managed = set_iambic_control(account.iambic_managed)
        account.partition = set_aws_account_partition(account.partition)

        if not questionary.confirm("Keep these settings? ").ask():
            return

        self.config.aws.accounts.append(account)

    def configuration_wizard_aws_account_edit(self):
        account_names = [account.account_name for account in self.config.aws.accounts]
        account_id_to_config_elem_map = {
            account.account_id: elem
            for elem, account in enumerate(self.config.aws.accounts)
        }
        if len(account_names) > 1:
            action = questionary.autocomplete(
                "Which AWS Account would you like to edit?",
                choices=["Go back", *account_names],
            ).ask()
            if action == "Go back":
                return
            account = next(
                (
                    account
                    for account in self.config.aws.accounts
                    if account.account_name == action
                ),
                None,
            )
            if not account:
                log.debug("Could not find AWS Account")
                return
        else:
            account = self.config.aws.accounts[0]

        choices = ["Go back", "Update partition", "Update Iambic control"]
        if not account.org_id:
            choices.append("Update name")

        while True:
            action = questionary.select(
                "What would you like to do?",
                choices=choices,
            ).ask()
            if action == "Go back":
                return
            elif action == "Update name":
                account.account_name = questionary.text(
                    "What is the name of the AWS Account?",
                    default=account.account_name,
                ).ask()
            elif action == "Update partition":
                account.partition = set_aws_account_partition(account.partition)
            elif action == "Update Iambic control":
                account.iambic_managed = set_iambic_control(account.iambic_managed)

            self.config.aws.accounts[
                account_id_to_config_elem_map[account.account_id]
            ] = account
            self.config.write(self.config_path)

    def configuration_wizard_aws_accounts(self):
        while True:
            if self.config.aws and self.config.aws.accounts:
                action = questionary.select(
                    "What would you like to do?",
                    choices=["Go back", "Add", "Edit"],
                ).ask()
                if action == "Go back":
                    return
                elif action == "Add":
                    self.configuration_wizard_aws_account_add()
                elif action == "Edit":
                    self.configuration_wizard_aws_account_edit()
            else:
                self.configuration_wizard_aws_account_add()

            self.config.write(self.config_path)

    def configuration_wizard_aws_organizations_edit(self):
        org_ids = [org.org_id for org in self.config.aws.organizations]
        org_id_to_config_elem_map = {
            org.org_id: elem for elem, org in enumerate(self.config.aws.organizations)
        }
        if len(org_ids) > 1:
            action = questionary.select(
                "Which AWS Organization would you like to edit?",
                choices=["Go back", *org_ids],
            ).ask()
            if action == "Go back":
                return
            org_to_edit = next(
                (org for org in self.config.aws.organizations if org.org_id == action),
                None,
            )
            if not org_to_edit:
                log.debug("Could not find AWS Organization to edit", org_id=action)
                return
        else:
            org_to_edit = self.config.aws.organizations[0]

        choices = [
            "Go back",
            "Update IdentityCenter",
            "Update Iambic control",
            "Update assume role name",
        ]
        while True:
            action = questionary.select(
                "What would you like to do?",
                choices=choices,
            ).ask()
            if action == "Go back":
                return
            elif action == "Update IdentityCenter":
                account_id = asyncio.run(get_org_account_id(org_to_edit))
                org_to_edit.identity_center_account = set_identity_center_account(
                    account_id, org_to_edit.identity_center_account.region
                )
            elif action == "Update role name":
                org_to_edit.default_rule.assume_role_name = (
                    set_aws_org_assume_role_name(
                        org_to_edit.default_rule.assume_role_name
                    )
                )
            elif action == "Update Iambic control":
                org_to_edit.iambic_managed = set_iambic_control(
                    org_to_edit.iambic_managed
                )

            self.config.aws.organizations[
                org_id_to_config_elem_map[org_to_edit.org_id]
            ] = org_to_edit
            self.config.write(self.config_path)

    def configuration_wizard_aws_organizations_add(self):
        org_region = questionary.text(
            "What region is the AWS Organization in? (Such as `us-east-1`)",
            default=self.default_region,
        ).ask()
        org_console_url = f"https://{org_region}.console.aws.amazon.com/organizations/v2/home/accounts"
        org_id = questionary.text(
            f"What is the AWS Organization ID? It can be found here {org_console_url}",
            default=self.autodetected_org_settings.get("Id"),
        ).ask()

        aws_org = AWSOrganization(
            org_id=org_id, region=org_region, default_rule=BaseAWSOrgRule()
        )
        if aws_profile := questionary.text(
            "(Optional) Provide an AWS profile name (Configured in ~/.aws/config) that Iambic "
            "should use when we access the AWS Organization. Note: "
            "this profile will be used before assuming any subsequent roles. "
            "(If you are using a role with access to the Organization and don't use "
            "a profile, you can leave this blank)",
            default=os.environ.get("AWS_PROFILE"),
        ).ask():
            aws_org.aws_profile = aws_profile

        if assume_role_arn := questionary.text(
            "(Optional) Provide a role ARN to assume when accessing the AWS Organization"
            " from the current role. This is required if running IAMbic from a githook or CI/CD pipeline."
        ).ask():
            aws_org.assume_role_arn = assume_role_arn

        aws_org.default_rule.iambic_managed = set_iambic_control(
            aws_org.default_rule.iambic_managed
        )

        self.config.aws.organizations.append(aws_org)

        log.debug("Attempting to get a session on the AWS org", org_id=org_id)
        try:
            session = asyncio.run(aws_org.get_boto3_session())
        except ClientError as e:
            log.error("Unable to get a session on the AWS org", org_id=org_id, error=e)
            session = None

        if (
            session
            and questionary.confirm(
                "Would you like to setup Identity Center (SSO) support?", default=False
            ).ask()
        ):
            account_id = asyncio.run(get_org_account_id(aws_org))
            aws_org.identity_center_account = set_identity_center_account(account_id)

        if not questionary.confirm("Keep these settings? ").ask():
            return

        if (
            session
            and questionary.confirm(
                "Bootstrap an Iambic Spoke Role on the org account?",
            ).ask()
        ):
            role_name = SPOKE_ROLE_TEMPLATE.properties.role_name
            self.config.aws.organizations[0].default_rule.assume_role_name = role_name
            asyncio.run(SPOKE_ROLE_TEMPLATE.apply(self.config, ctx))
            return

        self.config.aws.organizations[
            0
        ].default_rule.assume_role_name = set_aws_org_assume_role_name(
            aws_org.default_rule.assume_role_name
        )

    def configuration_wizard_aws_organizations(self):
        # Currently only 1 org per config is supported.
        if self.config.aws and self.config.aws.organizations:
            self.configuration_wizard_aws_organizations_edit()
        else:
            self.configuration_wizard_aws_organizations_add()

        self.config.write(self.config_path)

    def configuration_wizard_aws(self):
        while True:
            action = questionary.select(
                "What would you like to configure in AWS? "
                "We recommend configuring Iambic with AWS Organizations, "
                "but you may also manually configure accounts.",
                choices=["Go back", "AWS Organizations", "AWS Accounts"],
            ).ask()
            if action == "Go back":
                return
            elif action == "AWS Organizations":
                self.configuration_wizard_aws_organizations()
            elif action == "AWS Accounts":
                self.configuration_wizard_aws_accounts()

    def create_secret(self):
        region = questionary.text(
            "What region should the secret be created in? Example: `us-east-1`",
            default="us-east-1",
        ).ask()

        if (
            self.config.aws.organizations
            and self.config.aws.organizations[0].assume_role_arn
        ):
            role_arn = self.config.aws.organizations[0].assume_role_arn
        else:
            role_arn = questionary.text(
                "What is the ARN of the role that Iambic will assume to decode the secret? "
                "Example: `arn:aws:iam::123456789012:role/iambic`",
            ).ask()

        question_text = "Create the secret"
        role_name = role_arn.split("/")[-1] if role_arn else None
        role_account_id = role_arn.split(":")[4] if role_arn else None

        if role_name:
            question_text += f" and update the {role_name} template"

        if not questionary.confirm(f"{question_text}?").ask():
            self.config.secrets = {}
            return

        if role_name and (aws_account := self.aws_account_map.get(role_account_id)):
            session = asyncio.run(aws_account.get_boto3_session(region_name=region))
        else:
            session = boto3.Session(region_name=region)

        client = session.client(service_name="secretsmanager")
        response = client.create_secret(
            Name="iambic-config-test4",
            Description="IAMbic managed secret used to store protected config values",
            SecretString=yaml.dump({"secrets": self.config.secrets}),
        )

        if role_arn:
            role_template: RoleTemplate = self.existing_role_template_map.get(role_name)
            role_template.properties.inline_policies.append(
                PolicyDocument(
                    policy_name="read-iambic-secrets",
                    included_accounts=[role_account_id],
                    statement=[
                        PolicyStatement(
                            effect="Allow",
                            action=["secretsmanager:GetSecretValue"],
                            resource=[response["ARN"]],
                        )
                    ],
                )
            )
            role_template.write(exclude_unset=False)

        self.config.extends = [
            ExtendsConfig(
                key=ExtendsConfigKey.AWS_SECRETS_MANAGER,
                value=response["ARN"],
                assume_role_arn=role_arn,
            )
        ]

    def update_secret(self):
        self.config.secrets = {}
        if self.config.okta_organizations:
            self.config.secrets["okta"] = [
                org.dict() for org in self.config.okta_organizations
            ]

        if self.config.google_projects:
            self.config.secrets["google"] = [
                project.dict(
                    include={
                        "subjects",
                        "type",
                        "project_id",
                        "private_key_id",
                        "private_key",
                        "client_email",
                        "client_id",
                        "auth_uri",
                        "token_uri",
                        "auth_provider_x509_cert_url",
                        "client_x509_cert_url",
                    }
                )
                for project in self.config.google_projects
            ]

        secret_details = self.config.extends[0]
        secret_arn = secret_details.value
        region = secret_arn.split(":")[3]
        secret_account_id = secret_arn.split(":")[4]

        if aws_account := self.aws_account_map.get(secret_account_id):
            session = asyncio.run(aws_account.get_boto3_session(region_name=region))
        else:
            session = boto3.Session(region_name=region)

        client = session.client(service_name="secretsmanager")
        client.put_secret_value(
            SecretId=secret_arn,
            SecretString=yaml.dump({"secrets": self.config.secrets}),
        )

    def configuration_wizard_google_project_add(self):
        google_obj = {
            "subjects": [set_google_subject()],
            "type": set_google_project_type(),
            "project_id": set_google_project_id(),
            "private_key_id": set_google_private_key_id(),
            "private_key": set_google_private_key(),
            "client_email": set_google_client_email(),
            "client_id": set_google_client_id(),
            "auth_uri": set_google_auth_uri(),
            "token_uri": set_google_token_uri(),
            "auth_provider_x509_cert_url": set_google_auth_provider_cert_url(),
            "client_x509_cert_url": set_google_client_cert_url(),
        }
        if self.config.secrets:
            self.config.secrets.setdefault("google", []).append(google_obj)
            self.config.google_projects.append(GoogleProject(**google_obj))
            self.update_secret()
        else:
            self.config.secrets = {"google": [google_obj]}
            self.create_secret()

    def configuration_wizard_google_project_edit(self):
        project_ids = [project.project_id for project in self.config.google_projects]
        project_id_to_config_elem_map = {
            project.project_id: elem
            for elem, project in enumerate(self.config.google_projects)
        }
        if len(project_ids) > 1:
            action = questionary.select(
                "Which Google Project would you like to edit?",
                choices=["Go back", *project_ids],
            ).ask()
            if action == "Go back":
                return
            project_to_edit = next(
                (
                    project
                    for project in self.config.google_projects
                    if project.project_id == action
                ),
                None,
            )
            if not project_to_edit:
                log.debug("Could not find AWS Organization to edit", org_id=action)
                return
        else:
            project_to_edit = self.config.google_projects[0]

        project_id = project_to_edit.project_id
        choices = [
            "Go back",
            "Update Subject",
            "Update Type",
            "Update Private Key" "Update Private Key ID",
            "Update Client Email",
            "Update Client ID",
            "Update Auth URI",
            "Update Token URI",
            "Update Auth Provider Cert URL",
            "Update Client Cert URL",
        ]
        while True:
            action = questionary.select(
                "What would you like to do?",
                choices=choices,
            ).ask()
            if action == "Go back":
                return
            elif action == "Update Subject":
                if project_to_edit.subjects:
                    default_domain = project_to_edit.subjects[0].domain
                    default_service = project_to_edit.subjects[0].service
                else:
                    default_domain = None
                    default_service = None
                project_to_edit.subjects = [
                    set_google_subject(default_domain, default_service)
                ]
            elif action == "Update Type":
                project_to_edit.type = set_google_project_type(project_to_edit.type)
            elif action == "Update Private Key":
                project_to_edit.private_key = set_google_private_key(
                    project_to_edit.private_key
                )
            elif action == "Update Private Key ID":
                project_to_edit.private_key_id = set_google_private_key_id(
                    project_to_edit.private_key_id
                )
            elif action == "Update Client Email":
                project_to_edit.client_email = set_google_client_email(
                    project_to_edit.client_email
                )
            elif action == "Update Client ID":
                project_to_edit.client_id = set_google_client_id(
                    project_to_edit.client_id
                )
            elif action == "Update Auth URI":
                project_to_edit.auth_uri = set_google_auth_uri(project_to_edit.auth_uri)
            elif action == "Update Token URI":
                project_to_edit.token_uri = set_google_token_uri(
                    project_to_edit.token_uri
                )
            elif action == "Update Auth Provider Cert URL":
                project_to_edit.auth_provider_x509_cert_url = (
                    set_google_auth_provider_cert_url(
                        project_to_edit.auth_provider_x509_cert_url
                    )
                )
            elif action == "Update Client Cert URL":
                project_to_edit.client_x509_cert_url = set_google_client_cert_url(
                    project_to_edit.client_x509_cert_url
                )

            self.config.google_projects[
                project_id_to_config_elem_map[project_id]
            ] = project_to_edit
            self.update_secret()
            self.config.write(self.config_path)

    def configuration_wizard_google(self):
        if self.config.google_projects:
            action = questionary.select(
                "What would you like to do?",
                choices=["Go back", "Add", "Edit"],
            ).ask()
            if action == "Go back":
                return
            elif action == "Add":
                self.configuration_wizard_google_project_add()
            elif action == "Edit":
                self.configuration_wizard_google_project_edit()
        else:
            self.configuration_wizard_google_project_add()

    def configuration_wizard_okta_organization_add(self):
        okta_obj = {
            "idp_name": set_okta_idp_name(),
            "org_url": set_okta_org_url(),
            "api_token": set_okta_api_token(),
        }
        if self.config.secrets:
            self.config.secrets.setdefault("okta", []).append(okta_obj)
            self.config.okta_organizations.append(OktaOrganization(**okta_obj))
            self.update_secret()
        else:
            self.config.secrets = {"okta": [okta_obj]}
            self.create_secret()

    def configuration_wizard_okta_organization_edit(self):
        org_names = [org.idp_name for org in self.config.okta_organizations]
        org_name_to_config_elem_map = {
            org.idp_name: elem
            for elem, org in enumerate(self.config.okta_organizations)
        }
        if len(org_names) > 1:
            action = questionary.select(
                "Which Okta Organization would you like to edit?",
                choices=["Go back", *org_names],
            ).ask()
            if action == "Go back":
                return
            org_to_edit = next(
                (
                    org
                    for org in self.config.okta_organizations
                    if org.idp_name == action
                ),
                None,
            )
            if not org_to_edit:
                log.debug("Could not find Okta Organization to edit", idp_name=action)
                return
        else:
            org_to_edit = self.config.okta_organizations[0]

        org_name = org_to_edit.idp_name
        choices = [
            "Go back",
            "Update name",
            "Update Organization URL",
            "Update API Token",
        ]
        while True:
            action = questionary.select(
                "What would you like to do?",
                choices=choices,
            ).ask()
            if action == "Go back":
                return
            elif action == "Update name":
                org_to_edit.idp_name = set_okta_idp_name(org_to_edit.idp_name)
            elif action == "Update Organization URL":
                org_to_edit.org_url = set_okta_org_url(org_to_edit.org_url)
            elif action == "Update API Token":
                org_to_edit.api_token = set_okta_api_token(org_to_edit.api_token)

            self.config.okta_organizations[
                org_name_to_config_elem_map[org_name]
            ] = org_to_edit
            self.update_secret()
            self.config.write(self.config_path)

    def configuration_wizard_okta(self):
        if self.config.okta_organizations:
            action = questionary.select(
                "What would you like to do?",
                choices=["Go back", "Add", "Edit"],
            ).ask()
            if action == "Go back":
                return
            elif action == "Add":
                self.configuration_wizard_okta_organization_add()
            elif action == "Edit":
                self.configuration_wizard_okta_organization_edit()
        else:
            self.configuration_wizard_okta_organization_add()

    def configuration_wizard_github_workflow(self):
        print(
            "NOTE: Currently, only GitHub Workflows are supported. "
            "However, you can modify the generated output to work with your Git provider."
        )

        if questionary.confirm("Proceed?").ask():
            commit_email = set_required_text_value("E-Mail address to use for commits")
            repo_name = set_required_text_value(
                "Name of the repository, including the organization."
            )
            if self.config.aws and self.config.aws.organizations:
                aws_org = self.config.aws.organizations[0]
                assume_role_arn = aws_org.assume_role_arn
                region = aws_org.default_region

                if not assume_role_arn:
                    assume_role_arn = set_required_text_value(
                        "ARN of the role to assume for the workflow"
                    )
                    if questionary.confirm(
                        "Update the AWS Org definition in the config to include this value?"
                    ).ask():
                        self.config.aws.organizations[
                            0
                        ].assume_role_arn = assume_role_arn
                        self.config.write(self.config_path)
            else:
                region = set_required_text_value(
                    "What region should the workflow run in?", default_val="us-east-1"
                )
                assume_role_arn = set_required_text_value(
                    "ARN of the role to assume for the workflow"
                )

            create_workflow_files(
                repo_dir=self.repo_dir,
                repo_name=repo_name,
                commit_email=commit_email,
                assume_role_arn=assume_role_arn,
                region=region,
            )

    async def configuration_wizard_change_detection_setup(
        self, aws_org: AWSOrganization
    ):
        if not questionary.confirm(
            "To setup change detection for iambic requires "
            "creating CloudFormation stacks "
            "and a CloudFormation stack set. Proceed?"
        ).ask():
            return

        account = self.aws_account_map[asyncio.run(get_org_account_id(aws_org))]
        cf_client = await account.get_boto3_client("cloudformation", "us-east-1")
        org_client = await account.get_boto3_client("organizations", "us-east-1")
        role_arn = None

        if questionary.confirm(
            "Provide a role arn to execute the cloudformation used to provision"
        ).ask():
            role_arn = questionary.text(
                "What is the Role ARN to be used for creating the CloudFormations?"
            ).ask()

        successfully_created = await create_iambic_stacks(
            cf_client, org_client, account.org_id, account.account_id, role_arn
        )
        if not successfully_created:
            return

        # Update the role template by adding policy that allows the role to consume from the SQS queue

    def run(self):
        while True:
            choices = ["AWS", "Done"]
            secret_in_config = bool(self.config.extends)
            if secret_in_config:
                secret_question_text = "This requires the ability to update the AWS Secrets Manager secret."
            else:
                secret_question_text = "This requires permissions to update a role and create an AWS Secret."

            if self.config.aws:
                self.existing_role_template_map = asyncio.run(
                    get_existing_template_map(self.repo_dir, AWS_IAM_ROLE_TEMPLATE_TYPE)
                )
                if self.existing_role_template_map:
                    choices = [
                        "AWS",
                        "Google",
                        "Okta",
                        "Generate Git Workflows",
                        "Done",
                    ]

                if (
                    self.config.aws.organizations
                    and not self.config.sqs_cloudtrail_changes_queues
                ):
                    choices.insert(-1, "Setup Change Detection")

            action = questionary.select(
                "What would you to configure?",
                choices=choices,
            ).ask()

            # Let's try really hard not to use a switch statement since it depends on Python 3.10
            if action == "Done":
                self.config.write(self.config_path)
                return
            elif action == "AWS":
                self.configuration_wizard_aws()
                asyncio.run(self.attempt_aws_account_refresh())
                if questionary.confirm(
                    "Would you like to import your AWS identities?"
                ).ask():
                    for account in self.config.aws.accounts:
                        if account.identity_center_details:
                            asyncio.run(account.set_identity_center_details())
                    asyncio.run(
                        generate_aws_role_templates(
                            [self.config],
                            self.repo_dir,
                        )
                    )
            elif action == "Google":
                if questionary.confirm(f"{secret_question_text} Proceed?").ask():
                    self.configuration_wizard_google()
            elif action == "Okta":
                if questionary.confirm(f"{secret_question_text} Proceed?").ask():
                    self.configuration_wizard_okta()
            elif action == "Generate Git Workflows":
                self.configuration_wizard_github_workflow()
            elif action == "Setup AWS change detection":
                asyncio.run(
                    self.configuration_wizard_change_detection_setup(
                        self.config.aws.organizations[0]
                    )
                )
