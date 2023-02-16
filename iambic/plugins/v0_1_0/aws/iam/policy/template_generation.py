from __future__ import annotations

import itertools
import os
import pathlib
from collections import defaultdict
from typing import TYPE_CHECKING

import aiofiles

from iambic.core import noq_json as json
from iambic.core.logger import log
from iambic.core.template_generation import (
    base_group_str_attribute,
    create_or_update_template,
    get_existing_template_map,
    group_dict_attribute,
    group_int_or_str_attribute,
)
from iambic.core.utils import NoqSemaphore, get_writable_directory, resource_file_upsert
from iambic.plugins.v0_1_0.aws.event_bridge.models import ManagedPolicyMessageDetails
from iambic.plugins.v0_1_0.aws.iam.policy.models import (
    ManagedPolicyProperties,
    ManagedPolicyTemplate,
)
from iambic.plugins.v0_1_0.aws.iam.policy.utils import (
    get_managed_policy_across_accounts,
    list_managed_policies,
)
from iambic.plugins.v0_1_0.aws.models import AWSAccount
from iambic.plugins.v0_1_0.aws.utils import get_aws_account_map, normalize_boto3_resp

if TYPE_CHECKING:
    from iambic.plugins.v0_1_0.aws.iambic_plugin import AWSConfig


def get_managed_policy_response_dir() -> pathlib.Path:
    return get_writable_directory().joinpath(
        ".iambic", "resources", "aws", "managed_policies"
    )


def get_managed_policy_dir(base_dir: str) -> str:
    return str(os.path.join(base_dir, "resources", "aws", "managed_policies"))


def get_templated_managed_policy_file_path(
    managed_policy_dir: str,
    policy_name: str,
    included_accounts: list[str],
    account_map: dict[str, AWSAccount],
):
    if len(included_accounts) > 1:
        separator = "multi_account"
    elif included_accounts == ["*"] or included_accounts is None:
        separator = "all_accounts"
    else:
        separator = included_accounts[0]
    file_name = (
        policy_name.replace("{{", "")
        .replace("}}_", "_")
        .replace("}}", "_")
        .replace(".", "_")
        .lower()
    )
    return str(os.path.join(managed_policy_dir, separator, f"{file_name}.yaml"))


def get_account_managed_policy_resource_dir(account_id: str) -> str:
    account_resource_dir = os.path.join(get_managed_policy_response_dir(), account_id)
    os.makedirs(account_resource_dir, exist_ok=True)
    return account_resource_dir


async def generate_account_managed_policy_resource_files(
    aws_account: AWSAccount,
) -> dict:
    account_resource_dir = get_account_managed_policy_resource_dir(
        aws_account.account_id
    )
    resource_file_upsert_semaphore = NoqSemaphore(resource_file_upsert, 10)
    messages = []

    response = dict(account_id=aws_account.account_id, managed_policies=[])
    iam_client = await aws_account.get_boto3_client("iam")
    account_managed_policies = await list_managed_policies(iam_client)

    log.info(
        "Retrieved AWS IAM Managed Policies.",
        account_id=aws_account.account_id,
        account_name=aws_account.account_name,
        managed_policy_count=len(account_managed_policies),
    )

    for managed_policy in account_managed_policies:
        policy_path = os.path.join(
            account_resource_dir, f'{managed_policy["PolicyName"]}.json'
        )
        response["managed_policies"].append(
            {
                "file_path": policy_path,
                "policy_name": managed_policy["PolicyName"],
                "arn": managed_policy["Arn"],
                "account_id": aws_account.account_id,
            }
        )
        messages.append(
            dict(
                file_path=policy_path, content_as_dict=managed_policy, replace_file=True
            )
        )

    await resource_file_upsert_semaphore.process(messages)
    log.info(
        "Finished caching AWS IAM Managed Policies.",
        account_id=aws_account.account_id,
        managed_policy_count=len(account_managed_policies),
    )

    return response


async def generate_managed_policy_resource_file_for_all_accounts(
    aws_accounts: list[AWSAccount], policy_path: str, policy_name: str
) -> list:
    account_mp_response_dir_map = {
        aws_account.account_id: get_account_managed_policy_resource_dir(
            aws_account.account_id
        )
        for aws_account in aws_accounts
    }
    mp_resource_file_upsert_semaphore = NoqSemaphore(resource_file_upsert, 10)
    messages = []
    response = []

    mp_across_accounts = await get_managed_policy_across_accounts(
        aws_accounts, policy_path, policy_name
    )
    mp_across_accounts = {k: v for k, v in mp_across_accounts.items() if v}

    log.info(
        "Retrieved AWS IAM Managed Policy for all accounts.",
        policy_name=policy_name,
        policy_path=policy_path,
        total_accounts=len(mp_across_accounts),
    )

    for account_id, managed_policy in mp_across_accounts.items():
        policy_path = os.path.join(
            account_mp_response_dir_map[account_id],
            f'{managed_policy["PolicyName"]}.json',
        )

        response.append(
            {
                "file_path": policy_path,
                "policy_name": managed_policy["PolicyName"],
                "arn": managed_policy["Arn"],
                "account_id": account_id,
            }
        )
        messages.append(
            dict(
                file_path=policy_path, content_as_dict=managed_policy, replace_file=True
            )
        )

    await mp_resource_file_upsert_semaphore.process(messages)
    log.info(
        "Finished caching AWS IAM Managed Policy for all accounts.",
        policy_name=policy_name,
        policy_path=policy_path,
        total_accounts=len(mp_across_accounts),
    )

    return response


async def create_templated_managed_policy(  # noqa: C901
    aws_account_map: dict[str, AWSAccount],
    managed_policy_name: str,
    managed_policy_refs: list[dict],
    managed_policy_dir: str,
    existing_template_map: dict,
    config: AWSConfig,
):
    min_accounts_required_for_wildcard_included_accounts = (
        config.min_accounts_required_for_wildcard_included_accounts
    )
    account_id_to_mp_map = {}
    num_of_accounts = len(managed_policy_refs)
    for managed_policy_ref in managed_policy_refs:
        async with aiofiles.open(managed_policy_ref["file_path"], mode="r") as f:
            content_dict = json.loads(await f.read())
            account_id_to_mp_map[
                managed_policy_ref["account_id"]
            ] = normalize_boto3_resp(content_dict)

    # Generate the params used for attribute creation
    template_properties = {"policy_name": managed_policy_name}

    # TODO: Fix identifier it should be something along the lines of v but path can vary by account
    #       f"arn:aws:iam::{account_id}:policy{resource['Path']}{managed_policy_name}"
    template_params = {"identifier": managed_policy_name}
    path_resources = list()
    description_resources = list()
    policy_document_resources = list()
    tag_resources = list()
    for account_id, managed_policy_dict in account_id_to_mp_map.items():
        path_resources.append(
            {
                "account_id": account_id,
                "resources": [{"resource_val": managed_policy_dict["path"]}],
            }
        )
        policy_document_resources.append(
            {
                "account_id": account_id,
                "resources": [{"resource_val": managed_policy_dict["policy_document"]}],
            }
        )

        if tags := managed_policy_dict.get("tags"):
            tag_resources.append(
                {
                    "account_id": account_id,
                    "resources": [{"resource_val": tag} for tag in tags],
                }
            )

        if description := managed_policy_dict.get("description"):
            description_resources.append(
                {"account_id": account_id, "resources": [{"resource_val": description}]}
            )

    if (
        len(managed_policy_refs) != len(aws_account_map)
        or len(aws_account_map) <= min_accounts_required_for_wildcard_included_accounts
    ):
        template_params["included_accounts"] = [
            aws_account_map[managed_policy_ref["account_id"]].account_name
            for managed_policy_ref in managed_policy_refs
        ]
    else:
        template_params["included_accounts"] = ["*"]

    path = await group_int_or_str_attribute(
        aws_account_map, num_of_accounts, path_resources, "path"
    )
    if path != "/":
        template_properties["path"] = path

    template_properties["policy_document"] = await group_dict_attribute(
        aws_account_map, num_of_accounts, policy_document_resources, True
    )

    if description_resources:
        template_properties["description"] = await group_int_or_str_attribute(
            aws_account_map, num_of_accounts, description_resources, "description"
        )

    if tag_resources:
        tags = await group_dict_attribute(
            aws_account_map, num_of_accounts, tag_resources, True
        )
        if isinstance(tags, dict):
            tags = [tags]
        template_properties["tags"] = tags

    file_path = get_templated_managed_policy_file_path(
        managed_policy_dir,
        managed_policy_name,
        template_params.get("included_accounts"),
        aws_account_map,
    )
    return create_or_update_template(
        file_path,
        existing_template_map,
        managed_policy_name,
        ManagedPolicyTemplate,
        template_params,
        ManagedPolicyProperties(**template_properties),
        list(aws_account_map.values()),
    )


async def generate_aws_managed_policy_templates(
    config: AWSConfig,
    base_output_dir: str,
    managed_policy_messages: list[ManagedPolicyMessageDetails] = None,
):
    aws_account_map = await get_aws_account_map(config)
    existing_template_map = await get_existing_template_map(
        base_output_dir, "NOQ::IAM::ManagedPolicy"
    )
    resource_dir = get_managed_policy_dir(base_output_dir)
    generate_account_managed_policy_resource_files_semaphore = NoqSemaphore(
        generate_account_managed_policy_resource_files, 25
    )

    log.info("Generating AWS managed policy templates.")
    log.info(
        "Beginning to retrieve AWS IAM Managed Policies.",
        accounts=list(aws_account_map.keys()),
    )

    if managed_policy_messages:
        aws_accounts = list(aws_account_map.values())
        generate_mp_resource_file_for_all_accounts_semaphore = NoqSemaphore(
            generate_managed_policy_resource_file_for_all_accounts, 50
        )

        tasks = [
            {
                "aws_accounts": aws_accounts,
                "policy_path": managed_policy.policy_path,
                "policy_name": managed_policy.policy_name,
            }
            for managed_policy in managed_policy_messages
            if not managed_policy.delete
        ]

        # Remove deleted or mark templates for update
        deleted_managed_policies = [
            managed_policy
            for managed_policy in managed_policy_messages
            if managed_policy.delete
        ]
        if deleted_managed_policies:
            for managed_policy in deleted_managed_policies:
                policy_account = aws_account_map.get(managed_policy.account_id)
                if existing_template := existing_template_map.get(
                    managed_policy.policy_name
                ):
                    if len(existing_template.included_accounts) == 1 and (
                        existing_template.included_accounts[0]
                        == policy_account.account_name
                        or existing_template.included_accounts[0]
                        == policy_account.account_id
                    ):
                        # It's the only account for the template so delete it
                        existing_template.delete()
                    else:
                        # There are other accounts for the template so re-eval the template
                        tasks.append(
                            {
                                "aws_accounts": aws_accounts,
                                "policy_path": existing_template.properties.path,
                                "policy_name": existing_template.properties.policy_name,
                            }
                        )

        account_mp_list = (
            await generate_mp_resource_file_for_all_accounts_semaphore.process(tasks)
        )
        account_mp_list = list(itertools.chain.from_iterable(account_mp_list))
        account_policy_map = defaultdict(list)
        for account_policy in account_mp_list:
            account_policy_map[account_policy["account_id"]].append(account_policy)
        account_managed_policies = [
            dict(account_id=account_id, managed_policies=account_managed_policies)
            for account_id, account_managed_policies in account_policy_map.items()
        ]

    else:
        account_managed_policies = (
            await generate_account_managed_policy_resource_files_semaphore.process(
                [
                    {"aws_account": aws_account}
                    for aws_account in aws_account_map.values()
                ]
            )
        )

        # Remove templates not in any AWS account
        all_policy_names = set(
            itertools.chain.from_iterable(
                [
                    [
                        managed_policy["policy_name"]
                        for managed_policy in account["managed_policies"]
                    ]
                    for account in account_managed_policies
                ]
            )
        )
        for existing_template in existing_template_map.values():
            if existing_template.properties.policy_name not in all_policy_names:
                existing_template.delete()

    # Upsert Managed Policies
    messages = []
    for account in account_managed_policies:
        for managed_policy in account["managed_policies"]:
            messages.append(
                {
                    "policy_name": managed_policy["policy_name"],
                    "arn": managed_policy["arn"],
                    "file_path": managed_policy["file_path"],
                    "aws_account": aws_account_map[managed_policy["account_id"]],
                }
            )

    log.info("Finished retrieving managed policy details")

    # Use these for testing `create_templated_managed_policy`
    # account_managed_policy_output = json.dumps(account_managed_policies)
    # with open("account_managed_policy_output.json", "w") as f:
    #     f.write(account_managed_policy_output)
    # with open("account_managed_policy_output.json") as f:
    #     account_managed_policies = json.loads(f.read())

    log.info("Grouping managed policies")
    # Move everything to required structure
    for account_mp_elem in range(len(account_managed_policies)):
        for mp_elem in range(
            len(account_managed_policies[account_mp_elem]["managed_policies"])
        ):
            policy_name = account_managed_policies[account_mp_elem]["managed_policies"][
                mp_elem
            ].pop("policy_name")
            account_managed_policies[account_mp_elem]["managed_policies"][mp_elem][
                "resource_val"
            ] = policy_name

        account_managed_policies[account_mp_elem][
            "resources"
        ] = account_managed_policies[account_mp_elem].pop("managed_policies", [])

    grouped_managed_policy_map = await base_group_str_attribute(
        aws_account_map, account_managed_policies
    )

    log.info("Writing templated managed policies")
    for policy_name, policy_refs in grouped_managed_policy_map.items():
        await create_templated_managed_policy(
            aws_account_map,
            policy_name,
            policy_refs,
            resource_dir,
            existing_template_map,
            config,
        )

    log.info("Finished templated managed policy generation")
