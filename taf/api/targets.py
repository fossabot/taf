import click
import os
import json
from collections import defaultdict
from pathlib import Path
from taf.api.metadata import update_snapshot_and_timestamp, update_target_metadata
from taf.api.roles import (
    _initialize_roles_and_keystore,
    add_role,
    add_role_paths,
    remove_paths,
)
from taf.constants import DEFAULT_RSA_SIGNATURE_SCHEME
from taf.exceptions import TAFError
from taf.git import GitRepository

import taf.repositoriesdb as repositoriesdb
from taf.auth_repo import AuthenticationRepository
from taf.repository_tool import Repository
from tuf.repository_tool import TARGETS_DIRECTORY_NAME


def add_target_repo(
    auth_path: str,
    target_path: str,
    target_name: str,
    role: str,
    library_dir: str,
    keystore: str,
    scheme: str = DEFAULT_RSA_SIGNATURE_SCHEME,
    custom=None,
):
    auth_repo = AuthenticationRepository(path=auth_path)
    if not auth_repo.is_git_repository_root:
        print(f"{auth_path} is not a git repository!")
        return
    if library_dir is None:
        library_dir = auth_repo.path.parent.parent

    if target_name is not None:
        target_repo = GitRepository(library_dir, target_name)
    elif target_path is not None:
        target_repo = GitRepository(path=target_path)
    else:
        raise TAFError(
            "Cannot add new target repository. Specify either target name (and library dir) or target path"
        )

    existing_roles = auth_repo.get_all_targets_roles()
    if role not in existing_roles:
        parent_role = input("Enter new role's parent role (targets): ")
        paths = input(
            "Enter a comma separated list of path delegated to the new role: "
        )
        paths = [path.strip() for path in paths.split(",") if len(path.strip())]
        keys_number = input("Enter the number of signing keys of the new role (1): ")
        keys_number = int(keys_number or 1)
        threshold = input("Enter signatures threshold of the new role (1): ")
        threshold = int(threshold or 1)
        yubikey = click.confirm("Sign the new role's metadata using yubikeys? ")
        if target_name not in paths:
            paths.append(target_name)

        add_role(
            auth_path,
            role,
            parent_role or "targets",
            paths,
            keys_number,
            threshold,
            yubikey,
            keystore,
            DEFAULT_RSA_SIGNATURE_SCHEME,
            commit=False,
            auth_repo=auth_repo,
        )
    else:
        print("Role already exists")
        add_role_paths([target_name], role, keystore, commit=False, auth_repo=auth_repo)

    # target repo should be added to repositories.json
    # delegation paths should be extended if role != targets
    # if the repository already exists, create a target file
    repositories_json = repositoriesdb.load_repositories_json(auth_repo)
    repositories = repositories_json["repositories"]
    if target_repo.name in repositories:
        print(f"{target_repo.name} already added to repositories.json. Overwriting")
    repositories[target_repo.name] = {}
    if custom:
        repositories[target_name]["custom"] = custom

    # update content of repositories.json before updating targets metadata
    Path(auth_repo.path, repositoriesdb.REPOSITORIES_JSON_NAME).write_text(
        json.dumps(repositories_json, indent=4)
    )

    added_targets_data = {}
    if target_repo.is_git_repository_root:
        _save_top_commit_of_repo_to_target(
            library_dir, target_repo.name, auth_repo.path
        )
        added_targets_data[target_repo.name] = {}

    removed_targets_data = {}
    added_targets_data[repositoriesdb.REPOSITORIES_JSON_PATH] = {}
    update_target_metadata(
        auth_repo,
        added_targets_data,
        removed_targets_data,
        keystore,
        roles_infos=None,
        write=False,
        scheme=scheme,
    )

    # update snapshot and timestamp calls write_all, so targets updates will be saved too
    update_snapshot_and_timestamp(auth_repo, keystore, None, scheme=scheme)
    commit_message = input("\nEnter commit message and press ENTER\n\n")
    auth_repo.commit(commit_message)


def export_targets_history(repo_path, commit=None, output=None, target_repos=None):
    auth_repo = AuthenticationRepository(path=repo_path)
    commits = auth_repo.all_commits_since_commit(commit, auth_repo.default_branch)
    if not len(target_repos):
        target_repos = None
    else:
        repositoriesdb.load_repositories(auth_repo)
        invalid_targets = []
        for target_repo in target_repos:
            if repositoriesdb.get_repository(auth_repo, target_repo) is None:
                invalid_targets.append(target_repo)
        if len(invalid_targets):
            print(
                f"The following target repositories are not defined: {', '.join(invalid_targets)}"
            )
            return

    commits_on_branches = auth_repo.sorted_commits_and_branches_per_repositories(
        commits, target_repos
    )
    commits_json = json.dumps(commits_on_branches, indent=4)
    if output is not None:
        output = Path(output).resolve()
        if output.suffix != ".json":
            output = output.with_suffix(".json")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(commits_json)
        print(f"Result written to {output}")
    else:
        print(commits_json)


def list_targets(
    repo_path: str,
    library_dir: str,
):
    """
    <Purpose>
        Save the top commit of specified target repositories to the corresponding target files and sign
    <Arguments>
        repo_path:
        Authentication repository's location
        library_dir:
        Directory where target repositories and, optionally, authentication repository are locate
    """
    auth_path = Path(repo_path).resolve()
    auth_repo = AuthenticationRepository(path=auth_path)
    top_commit = [auth_repo.head_commit_sha()]
    if library_dir is None:
        library_dir = auth_path.parent.parent
    repositoriesdb.load_repositories(auth_repo)
    target_repositories = repositoriesdb.get_deduplicated_repositories(auth_repo)
    repositories_data = auth_repo.sorted_commits_and_branches_per_repositories(
        top_commit
    )
    output = defaultdict(dict)
    for repo_name, repo_data in repositories_data.items():
        repo = target_repositories[repo_name]
        local_repo_exists = repo.is_git_repository_root
        repo_output = {}
        output[repo_name] = repo_output
        repo_output["unauthenticated-allowed"] = repo.custom.get(
            "allow-unauthenticated-commits", False
        )
        repo_output["cloned"] = local_repo_exists
        if local_repo_exists:
            repo_output["bare"] = repo.is_bare_repository()
            # there will only be one branch since only data corresponding to the top auth commit was loaded
            for branch, branch_data in repo_data.items():
                branch_data = branch_data[0]
                repo_output["unsigned"] = False
                if not repo.branch_exists(branch, include_remotes=False):
                    repo_output["up-to-date"] = False
                else:
                    is_synced_with_remote = repo.synced_with_remote(branch=branch)
                    repo_output["up-to-date"] = is_synced_with_remote
                    if not is_synced_with_remote:
                        last_signed_commit = branch_data["commit"]
                        if branch in repo.branches_containing_commit(
                            last_signed_commit
                        ):
                            top_commit = repo.top_commit_of_branch(branch)
                            repo_output[
                                "unsigned"
                            ] = top_commit in repo.all_commits_since_commit(
                                last_signed_commit, branch
                            )
            repo_output["something-to-commit"] = repo.something_to_commit()

    print(json.dumps(output, indent=4))


def register_target_files(
    repo_path,
    keystore=None,
    roles_key_infos=None,
    commit=False,
    scheme=DEFAULT_RSA_SIGNATURE_SCHEME,
    taf_repo=None,
):
    """
    <Purpose>
        Register all files found in the target directory as targets - updates the targets
        metadata file, snapshot and timestamp. Sign targets
        with yubikey if keystore is not provided
    <Arguments>
        repo_path:
        Authentication repository's path
        keystore:
        Location of the keystore files
        roles_key_infos:
        A dictionary whose keys are role names, while values contain information about the keys.
        commit_msg:
        Commit message. If specified, the changes made to the authentication are committed.
        scheme:
        A signature scheme used for signing.
        taf_repo:
        If taf repository is already initialized, it can be passed and used.
    """
    print("Signing target files")
    roles_key_infos, keystore = _initialize_roles_and_keystore(
        roles_key_infos, keystore, enter_info=False
    )
    roles_infos = roles_key_infos.get("roles")
    if taf_repo is None:
        repo_path = Path(repo_path).resolve()
        taf_repo = Repository(str(repo_path))

    # find files that should be added/modified/removed
    added_targets_data, removed_targets_data = taf_repo.get_all_target_files_state()

    update_target_metadata(
        taf_repo,
        added_targets_data,
        removed_targets_data,
        keystore,
        roles_infos,
        scheme,
    )

    if commit:
        auth_git_repo = GitRepository(path=taf_repo.path)
        commit_message = input("\nEnter commit message and press ENTER\n\n")
        auth_git_repo.commit(commit_message)


def remove_target_repo(
    auth_path: str,
    target_name: str,
    keystore: str,
):
    auth_repo = AuthenticationRepository(path=auth_path)
    removed_targets_data = {}
    added_targets_data = {}
    if not auth_repo.is_git_repository_root:
        print(f"{auth_path} is not a git repository!")
        return
    repositories_json = repositoriesdb.load_repositories_json(auth_repo)
    repositories = repositories_json["repositories"]
    if target_name not in repositories:
        print(f"{target_name} not in repositories.json")
    else:
        repositories.pop(target_name)
        # update content of repositories.json before updating targets metadata
        Path(auth_repo.path, repositoriesdb.REPOSITORIES_JSON_PATH).write_text(
            json.dumps(repositories_json, indent=4)
        )
        added_targets_data[repositoriesdb.REPOSITORIES_JSON_NAME] = {}

    auth_repo_targets_dir = Path(auth_repo.path, TARGETS_DIRECTORY_NAME)
    target_file_path = auth_repo_targets_dir / target_name

    if target_file_path.is_file():
        os.unlink(str(target_file_path))
        removed_targets_data[target_name] = {}
    else:
        print(f"{target_file_path} target file does not exist")

    update_target_metadata(
        auth_repo,
        added_targets_data,
        removed_targets_data,
        keystore,
        roles_infos=None,
        write=False,
    )

    update_snapshot_and_timestamp(
        auth_repo, keystore, None, scheme=DEFAULT_RSA_SIGNATURE_SCHEME
    )
    auth_repo.commit(f"Remove {target_name} target")
    # commit_message = input("\nEnter commit message and press ENTER\n\n")

    remove_paths([target_name], keystore, commit=False, auth_repo=auth_repo)
    update_snapshot_and_timestamp(
        auth_repo, keystore, None, scheme=DEFAULT_RSA_SIGNATURE_SCHEME
    )
    auth_repo.commit(f"Remove {target_name} from delegated paths")
    # update snapshot and timestamp calls write_all, so targets updates will be saved too


def _save_top_commit_of_repo_to_target(
    library_dir: Path, repo_name: str, auth_repo_path: Path, add_branch: bool = True
):
    auth_repo_targets_dir = auth_repo_path / TARGETS_DIRECTORY_NAME
    target_repo_path = library_dir / repo_name
    namespace_and_name = repo_name.rsplit("/", 1)
    targets_dir = auth_repo_targets_dir
    if len(namespace_and_name) > 1:
        namespace, _ = namespace_and_name
        targets_dir = auth_repo_targets_dir / namespace
    targets_dir.mkdir(parents=True, exist_ok=True)
    _update_target_repos(auth_repo_path, targets_dir, target_repo_path, add_branch)


def update_target_repos_from_repositories_json(
    repo_path,
    library_dir,
    keystore,
    add_branch=True,
    scheme=DEFAULT_RSA_SIGNATURE_SCHEME,
):
    """
    <Purpose>
        Create or update target files by reading the latest commit's repositories.json
    <Arguments>
        repo_path:
        Authentication repository's location
        library_dir:
        Directory where target repositories and, optionally, authentication repository are locate
        namespace:
        Namespace used to form the full name of the target repositories. Each target repository
        add_branch:
        Indicates whether to add the current branch's name to the target file
    """
    repo_path = Path(repo_path).resolve()
    if library_dir is None:
        library_dir = repo_path.parent.parent
    else:
        library_dir = Path(library_dir)
    auth_repo_targets_dir = repo_path / TARGETS_DIRECTORY_NAME
    repositories_json = json.loads(
        Path(auth_repo_targets_dir / "repositories.json").read_text()
    )
    for repo_name in repositories_json.get("repositories"):
        _save_top_commit_of_repo_to_target(
            library_dir, repo_name, repo_path, add_branch
        )
    register_target_files(repo_path, keystore, None, True, scheme)


def update_and_sign_targets(
    repo_path: str,
    library_dir: str,
    target_types: list,
    keystore: str,
    roles_key_infos: str,
    scheme: str,
):
    """
    <Purpose>
        Save the top commit of specified target repositories to the corresponding target files and sign
    <Arguments>
        repo_path:
        Authentication repository's location
        library_dir:
        Directory where target repositories and, optionally, authentication repository are locate
        targets:
        Types of target repositories whose corresponding target files should be updated and signed
        keystore:
        Location of the keystore files
        roles_key_infos:
        A dictionary whose keys are role names, while values contain information about the keys
        no_commit:
        Indicates that the changes should bot get committed automatically
        scheme:
        A signature scheme used for signing

    """
    auth_path = Path(repo_path).resolve()
    auth_repo = AuthenticationRepository(path=auth_path)
    if library_dir is None:
        library_dir = auth_path.parent.parent
    repositoriesdb.load_repositories(auth_repo)
    nonexistent_target_types = []
    target_names = []
    for target_type in target_types:
        try:
            target_name = repositoriesdb.get_repositories_paths_by_custom_data(
                auth_repo, type=target_type
            )[0]
            target_names.append(target_name)
        except Exception:
            nonexistent_target_types.append(target_type)
            continue
    if len(nonexistent_target_types):
        print(
            f"Target types {'.'.join(nonexistent_target_types)} not in repositories.json. Targets not updated"
        )
        return

    # only update target files if all specified types are valid
    for target_name in target_names:
        _save_top_commit_of_repo_to_target(library_dir, target_name, auth_path, True)
        print(f"Updated {target_name} target file")
    register_target_files(auth_path, keystore, roles_key_infos, True, scheme)


def _update_target_repos(repo_path, targets_dir, target_repo_path, add_branch):
    """Updates target repo's commit sha and branch"""
    if not target_repo_path.is_dir() or target_repo_path == repo_path:
        return
    target_repo = GitRepository(path=target_repo_path)
    if target_repo.is_git_repository:
        data = {"commit": target_repo.head_commit_sha()}
        if add_branch:
            data["branch"] = target_repo.get_current_branch()
        target_repo_name = target_repo_path.name
        path = targets_dir / target_repo_name
        path.write_text(json.dumps(data, indent=4))
        print(f"Updated {path}")
