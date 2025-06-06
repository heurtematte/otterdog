#  *******************************************************************************
#  Copyright (c) 2023-2025 Eclipse Foundation and others.
#  This program and the accompanying materials are made available
#  under the terms of the Eclipse Public License 2.0
#  which is available at http://www.eclipse.org/legal/epl-v20.html
#  SPDX-License-Identifier: EPL-2.0
#  *******************************************************************************

from logging import getLogger

from pydantic import ValidationError
from quart import Response, current_app

from otterdog.utils import expect_type
from otterdog.webapp.blueprints import create_blueprint_from_model, is_blueprint_path
from otterdog.webapp.db.service import (
    find_blueprint,
    get_blueprints_status_for_repo,
    get_installation,
    update_installation_status,
    update_installations_from_config,
)
from otterdog.webapp.policies import create_policy_from_model, is_policy_path
from otterdog.webapp.tasks.apply_changes import ApplyChangesTask
from otterdog.webapp.tasks.blueprints.update_blueprint_status import UpdateBlueprintStatusTask
from otterdog.webapp.tasks.check_sync import CheckConfigurationInSyncTask
from otterdog.webapp.tasks.delete_branch import DeleteBranchTask
from otterdog.webapp.tasks.fetch_blueprints import FetchBlueprintsTask
from otterdog.webapp.tasks.fetch_config import FetchConfigTask
from otterdog.webapp.tasks.fetch_policies import FetchPoliciesTask
from otterdog.webapp.tasks.help_comment import HelpCommentTask
from otterdog.webapp.tasks.retrieve_team_membership import RetrieveTeamMembershipTask
from otterdog.webapp.tasks.update_pull_request import UpdatePullRequestTask
from otterdog.webapp.tasks.validate_pull_request import ValidatePullRequestTask
from otterdog.webapp.utils import refresh_global_blueprints, refresh_global_policies, refresh_otterdog_config

from .comment_handlers import (
    ApplyCommentHandler,
    CheckSyncCommentHandler,
    CommentHandler,
    DoneCommentHandler,
    HelpCommentHandler,
    MergeCommentHandler,
    TeamInfoCommentHandler,
    ValidateCommentHandler,
)
from .github_models import (
    InstallationEvent,
    IssueCommentEvent,
    PullRequestEvent,
    PullRequestReviewEvent,
    PushEvent,
    WorkflowJobEvent,
    WorkflowRunEvent,
    touched_by_commits,
)
from .github_webhook import GitHubWebhook

webhook = GitHubWebhook()

comment_handlers: list[CommentHandler] = [
    HelpCommentHandler(),
    TeamInfoCommentHandler(),
    CheckSyncCommentHandler(),
    DoneCommentHandler(),
    ApplyCommentHandler(),
    MergeCommentHandler(),
    ValidateCommentHandler(),
]

logger = getLogger(__name__)


@webhook.hook("pull_request")
async def on_pull_request_received(data):
    try:
        event = PullRequestEvent.model_validate(data)
    except ValidationError:
        logger.error("failed to load pull request event data", exc_info=True)
        return success()

    if event.installation is None or event.organization is None:
        return success()

    installation_id = event.installation.id
    owner = event.organization.login
    repo_name = event.repository.name

    if event.action in ["closed"] and event.pull_request.head.ref.startswith("otterdog/"):
        current_app.add_background_task(
            DeleteBranchTask(
                installation_id,
                owner,
                repo_name,
                event.pull_request.head.ref,
            )
        )

    if event.action in ["closed", "reopened"] and event.pull_request.head.ref.startswith("otterdog/"):
        current_app.add_background_task(
            UpdateBlueprintStatusTask(
                installation_id,
                owner,
                repo_name,
                event.pull_request,
            )
        )

    if not await targets_config_repo(repo_name, installation_id):
        return success()

    if event.action in [
        "opened",
        "closed",
        "ready_for_review",
        "converted_to_draft",
        "ready_for_review",
        "reopened",
        "synchronize",
    ]:
        current_app.add_background_task(
            UpdatePullRequestTask(
                installation_id,
                owner,
                repo_name,
                event.pull_request,
            )
        )

    if event.action in ["opened", "ready_for_review"] and event.pull_request.draft is False:
        current_app.add_background_task(
            HelpCommentTask(
                installation_id,
                owner,
                repo_name,
                event.pull_request,
            )
        )

        current_app.add_background_task(
            RetrieveTeamMembershipTask(
                installation_id,
                owner,
                repo_name,
                # use the PR number to retrieve all data in the task itself
                # author_association might contain invalid data otherwise
                event.pull_request.number,
            )
        )

    if (
        event.action in ["opened", "synchronize", "ready_for_review", "reopened"]
        and event.pull_request.state == "open"
        and event.pull_request.draft is False
    ):
        # schedule a validate task
        current_app.add_background_task(
            ValidatePullRequestTask(
                installation_id,
                owner,
                repo_name,
                event.pull_request,
            )
        )

        # schedule a check-sync task
        current_app.add_background_task(
            CheckConfigurationInSyncTask(
                installation_id,
                owner,
                repo_name,
                event.pull_request,
            )
        )

    elif event.action in ["closed"] and event.pull_request.merged is True:
        if event.pull_request.base.ref != event.repository.default_branch:
            return success()

        current_app.add_background_task(
            ApplyChangesTask(
                installation_id,
                owner,
                repo_name,
                event.pull_request,
            )
        )

    return success()


@webhook.hook("pull_request_review")
async def on_pull_request_review_received(data):
    try:
        event = PullRequestReviewEvent.model_validate(data)
    except ValidationError:
        logger.error("failed to load pull request review event data", exc_info=True)
        return success()

    if event.installation is None or event.organization is None:
        return success()

    installation_id = event.installation.id
    owner = event.organization.login
    repo_name = event.repository.name

    if not await targets_config_repo(event.repository.name, installation_id):
        return success()

    if event.action in ["submitted", "edited", "dismissed"]:
        current_app.add_background_task(
            UpdatePullRequestTask(
                installation_id,
                owner,
                repo_name,
                event.pull_request,
                event.review,
            )
        )

    return success()


@webhook.hook("issue_comment")
async def on_issue_comment_received(data):
    try:
        event = IssueCommentEvent.model_validate(data)
    except ValidationError:
        logger.error("failed to load issue comment event data", exc_info=True)
        return success()

    if event.installation is None or event.organization is None:
        return success()

    # currently we only handle comments to pull requests
    if event.issue.pull_request is None:
        return success()

    if not await targets_config_repo(event.repository.name, event.installation.id):
        return success()

    if event.action in ["created", "edited"]:
        for handler in comment_handlers:
            match = handler.matches(event.comment.body)
            if match is not None:
                handler.process(match, event)
                break

    return success()


@webhook.hook("push")
async def on_push_received(data):
    try:
        event = PushEvent.model_validate(data)
    except ValidationError:
        logger.error("failed to load push event data", exc_info=True)
        return success()

    # check if the push targets the default branch of the config repo of an installation,
    # in such a case, update the current config in the database
    if event.installation is not None and event.organization is not None:
        if event.ref != f"refs/heads/{event.repository.default_branch}":
            return success()

        installation_id = event.installation.id
        owner = event.organization.login
        repo_name = event.repository.name

        config_repo_touched = await targets_config_repo(repo_name, installation_id)

        async def fetch_config_and_check_blueprints_if_needed() -> None:
            if config_repo_touched:
                await FetchConfigTask(
                    installation_id,
                    owner,
                    repo_name,
                ).execute()

            # check any blueprint that matches the repo that just got a new push on the default branch
            for blueprint_status_model in await get_blueprints_status_for_repo(owner, repo_name):
                blueprint_model = await find_blueprint(owner, blueprint_status_model.id.blueprint_id)
                if blueprint_model is not None:
                    blueprint_instance = create_blueprint_from_model(blueprint_model)
                    if blueprint_instance.should_reevaluate(event.commits):
                        await blueprint_instance.evaluate_repo(installation_id, owner, repo_name)

        current_app.add_background_task(fetch_config_and_check_blueprints_if_needed)

        if not config_repo_touched:
            return success()

        policies_modified = touched_by_commits(is_policy_path, event.commits)
        if policies_modified is True:
            global_policies = await refresh_global_policies()
            current_app.add_background_task(
                FetchPoliciesTask(
                    installation_id,
                    owner,
                    repo_name,
                    global_policies,
                )
            )

        blueprints_modified = touched_by_commits(is_blueprint_path, event.commits)
        if blueprints_modified is True:
            global_blueprints = await refresh_global_blueprints()
            current_app.add_background_task(
                FetchBlueprintsTask(
                    installation_id,
                    owner,
                    repo_name,
                    global_blueprints,
                )
            )

        return success()

    # if the otterdog config repo has been update, update all installations
    if (
        event.repository.name == current_app.config["OTTERDOG_CONFIG_REPO"]
        and event.repository.owner.login == current_app.config["OTTERDOG_CONFIG_OWNER"]
    ):
        if event.ref != f"refs/heads/{event.repository.default_branch}":
            return success()

        async def update_installations() -> None:
            config = await refresh_otterdog_config(event.after)
            policies = await refresh_global_policies(event.after)
            blueprints = await refresh_global_blueprints(event.after)
            await update_installations_from_config(config, policies, blueprints)

        current_app.add_background_task(update_installations)
        return success()

    return success()


@webhook.hook("installation")
async def on_installation_received(data):
    try:
        event = InstallationEvent.model_validate(data)
    except ValidationError:
        logger.error("failed to load installation event data", exc_info=True)
        return success()

    current_app.add_background_task(update_installation_status, event.installation.id, event.action)
    return success()


@webhook.hook("workflow_job")
async def on_workflow_job_received(data):
    try:
        event = WorkflowJobEvent.model_validate(data)
    except ValidationError:
        logger.error("failed to load workflow job event data", exc_info=True)
        return success()

    if event.installation is None or event.organization is None:
        return success()

    installation_id = event.installation.id
    owner = event.organization.login

    if event.action in ["queued"]:
        logger.debug(f"workflow job queued on runner: {', '.join(event.workflow_job.labels)}")

        from otterdog.webapp.db.service import find_policy
        from otterdog.webapp.policies import PolicyType
        from otterdog.webapp.policies.macos_large_runners import (
            MacOSLargeRunnersUsagePolicy,
        )

        policy_model = await find_policy(owner, PolicyType.MACOS_LARGE_RUNNERS_USAGE.value)
        if policy_model is not None:
            policy = create_policy_from_model(policy_model)
            expect_type(policy, MacOSLargeRunnersUsagePolicy)
            await policy.evaluate(
                installation_id,
                owner,
                event.repository.name,
                event.workflow_job,
            )

    return success()


@webhook.hook("workflow_run")
async def on_workflow_run_received(data):
    try:
        event = WorkflowRunEvent.model_validate(data)
    except ValidationError:
        logger.error("failed to load workflow run event data", exc_info=True)
        return success()

    if event.installation is None or event.organization is None:
        return success()

    installation_id = event.installation.id
    owner = event.organization.login

    if event.action in ["completed"]:
        logger.debug(f"workflow run completed in repo: {event.repository.full_name}")

        from otterdog.webapp.db.service import find_policy
        from otterdog.webapp.policies import PolicyType
        from otterdog.webapp.policies.dependency_track_upload import (
            DependencyTrackUploadPolicy,
        )

        policy_model = await find_policy(owner, PolicyType.DEPENDENCY_TRACK_UPLOAD.value)
        if policy_model is not None:
            policy = create_policy_from_model(policy_model)
            expect_type(policy, DependencyTrackUploadPolicy)
            await policy.evaluate(
                installation_id,
                owner,
                event.repository.name,
                event.workflow_run,
            )


async def targets_config_repo(repo_name: str, installation_id: int) -> bool:
    installation = await get_installation(installation_id)
    if installation is None:
        logger.warning(f"received event for unknown installation '{installation_id}'")
        return False

    return repo_name == installation.config_repo


def success() -> Response:
    return Response({}, mimetype="application/json", status=200)
