#!/usr/bin/env python3
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


CONFIG_DIR = Path.home() / ".config" / "dir-merge"
STATE_FILE = CONFIG_DIR / "session.json"


def load_session_state() -> dict:
    """Load the session state from config. Error if none exists."""
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    print("Error: No active merge session", file=sys.stderr)
    sys.exit(1)


def save_session_state(state: dict) -> None:
    """Save the session state to config."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def clear_session_state() -> None:
    """Delete the session state file."""
    if STATE_FILE.exists():
        STATE_FILE.unlink()


def merge_command(args):
    """Start a merge session by initializing git repo with source as BASE."""
    # Validate directories exist
    if not args.source.is_dir():
        print(
            f"Error: source directory '{args.source}' does not exist", file=sys.stderr
        )
        sys.exit(1)

    if not args.target.is_dir():
        print(
            f"Error: target directory '{args.target}' does not exist", file=sys.stderr
        )
        sys.exit(1)

    # Resolve to absolute paths
    source_dir = args.source.resolve()
    target_dir = args.target.resolve()
    output_dir = (args.output or source_dir).resolve()
    original_cwd = Path.cwd()

    print("Starting merge session")
    print(f"  Source:  {source_dir}")
    print(f"  Target:  {target_dir}")
    print(f"  Output:  {output_dir}")
    print()

    try:
        # Create temporary directory (this is our session ID)
        temp_dir = Path(tempfile.mkdtemp(prefix="dir-merge-"))

        # Copy source into temp dir
        print(f"Copying source to temp repo: {temp_dir}")
        shutil.copytree(source_dir, temp_dir, dirs_exist_ok=True)

        # Initialize git repo
        print("Initializing git repository...")
        subprocess.run(
            ["git", "init"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        # Configure git
        subprocess.run(
            ["git", "config", "user.email", "dir-merge@local"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "dir-merge"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        # Add and commit source as BASE
        subprocess.run(
            ["git", "add", "."],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "BASE"],
            cwd=temp_dir,
            check=True,
            capture_output=True,
        )

        # Overlay target with rsync (delete extraneous files, but preserve .git)
        print("Overlaying target...")
        subprocess.run(
            [
                "rsync",
                "-av",
                "--delete",
                "--exclude=.git",
                f"{target_dir}/",
                f"{temp_dir}/",
            ],
            check=True,
            capture_output=True,
        )

        # Save session state
        state = {
            "temp_repo_path": str(temp_dir),
            "original_cwd": str(original_cwd),
            "output_dir": str(output_dir),
            "source_dir": str(source_dir),
            "target_dir": str(target_dir),
        }
        save_session_state(state)

        print()
        print("Merge session initialized!")
        print(f"Session ID (temp repo): {temp_dir.name}")
        print()
        print("Spawning new shell in temp directory...")
        print()

        # Spawn new interactive shell in temp directory
        shell = os.environ.get("SHELL", "/bin/bash")
        shell_process = subprocess.Popen(shell, cwd=temp_dir)

        # Store shell PID in session state
        state["shell_pid"] = str(shell_process.pid)
        save_session_state(state)

        # Wait for shell to exit
        shell_process.wait()

    except subprocess.CalledProcessError as e:
        print(f"Error: Command failed: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def finish_command(args):
    """Apply changes from merge session to output directory."""
    # Load session state
    state = load_session_state()

    # Use current pwd as temp_repo (must be run from within the spawned shell)
    current_cwd = Path.cwd().resolve()
    temp_repo = Path(state["temp_repo_path"]).resolve()
    output_dir = Path(state["output_dir"])

    # Verify we're in the temp repo directory
    if current_cwd != temp_repo:
        print(
            "Error: finish must be run from within the temp repository",
            file=sys.stderr,
        )
        print(f"Expected: {temp_repo}", file=sys.stderr)
        print(f"Current:  {current_cwd}", file=sys.stderr)
        sys.exit(1)

    # Validate temp repo still exists
    if not temp_repo.exists():
        print(f"Error: Temporary repository not found: {temp_repo}", file=sys.stderr)
        clear_session_state()
        sys.exit(1)

    print(f"Applying changes to {output_dir}")
    print()

    try:
        # Create output dir if needed
        output_dir.mkdir(parents=True, exist_ok=True)

        # Reset working directory to only include staged/committed changes
        # Discard unstaged modifications
        subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=temp_repo,
            check=True,
            capture_output=True,
        )
        # Remove untracked files
        subprocess.run(
            ["git", "clean", "-fd"],
            cwd=temp_repo,
            check=True,
            capture_output=True,
        )

        # Copy repo state to output (excluding .git)
        print("Copying repository state to output directory...")
        subprocess.run(
            [
                "rsync",
                "-av",
                "--delete",
                "--exclude=.git",
                f"{temp_repo}/",
                f"{output_dir}/",
            ],
            check=True,
            capture_output=True,
        )

        # Clean up temp directory
        print("Cleaning up temporary repository...")
        shutil.rmtree(temp_repo)

        # Get shell PID before clearing session state
        shell_pid = int(state.get("shell_pid", -1))

        # Clear session state
        clear_session_state()

        print()
        print("Successfully applied changes!")
        print(f"Output directory: {output_dir}")
        print()
        print("Exiting temp shell and returning to original directory...")

        # Kill the parent shell to exit the subshell
        if shell_pid > 0:
            os.kill(shell_pid, 9)

    except subprocess.CalledProcessError as e:
        print(f"Error: Command failed: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def status_command(args):
    """Show details of current merge session."""
    state = load_session_state()

    print("Active merge session:")
    print()
    print(f"  Session ID (temp repo):  {Path(state['temp_repo_path']).name}")
    print(f"  Temp repo path:          {state['temp_repo_path']}")
    print(f"  Original directory:      {state['original_cwd']}")
    print(f"  Output directory:        {state['output_dir']}")
    print(f"  Source directory:        {state['source_dir']}")
    print(f"  Target directory:        {state['target_dir']}")
    print()

    # Also show git status if possible
    temp_repo = Path(state["temp_repo_path"])
    if temp_repo.exists():
        print("Git status in temp repo:")
        print()
        try:
            result = subprocess.run(
                ["git", "status", "--short"],
                cwd=temp_repo,
                capture_output=True,
                text=True,
            )
            if result.stdout:
                print(result.stdout)
            else:
                print("  (no changes)")
        except Exception:
            pass
        print()


def main():
    parser = argparse.ArgumentParser(description="Simple directory merger using git")

    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # merge command
    merge_parser = subparsers.add_parser(
        "merge",
        help="Start a merge session (initialize git repo with source as BASE)",
    )
    merge_parser.add_argument("source", type=Path, help="Source directory (base)")
    merge_parser.add_argument("target", type=Path, help="Target directory (overlay)")
    merge_parser.add_argument(
        "--output",
        type=Path,
        help="Output directory for applied changes (defaults to source)",
    )
    merge_parser.set_defaults(func=merge_command)

    # finish command
    finish_parser = subparsers.add_parser(
        "finish",
        help="Apply changes to output directory and clean up",
    )
    finish_parser.set_defaults(func=finish_command)

    # status command
    status_parser = subparsers.add_parser(
        "status",
        help="Show current merge session details",
    )
    status_parser.set_defaults(func=status_command)

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    args.func(args)


if __name__ == "__main__":
    main()
