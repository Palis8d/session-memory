# Security and privacy

Session Memory keeps a searchable copy of your Claude Code and Codex history on your computer. This page lists what it promises, the test that checks each promise, and what it does and doesn't protect against.

## Promises, and the tests that check them

Run all tests with `python3 -I tests/test_package.py`, `tests/test_adapters.py` and `tests/test_redaction.py`. Run `python3 -I tests/break_on_purpose.py` to break each key rule in a throwaway copy and confirm a test notices.

| Promise | Checked by |
| --- | --- |
| It only reads the assistants' history. The one thing it adds in their folders is its own skill folder, and it never creates files in Codex's folder. | [`test_private_files_and_no_files_in_codex_folder`](tests/test_package.py), [`test_end_to_end_setup_read_only_use_and_uninstall`](tests/test_package.py), [`test_store_refuses_source_paths`](tests/test_adapters.py) |
| The memory folder and files are readable only by your account. | [`test_private_files_and_no_files_in_codex_folder`](tests/test_package.py) |
| Common forms of passwords, keys and tokens are replaced with `[REDACTED]` before anything is stored, and ordinary text is left alone. | [`test_review_cases_are_redacted`](tests/test_redaction.py), [`test_more_formats_are_redacted`](tests/test_redaction.py), [`test_ordinary_text_is_left_alone`](tests/test_redaction.py), [`test_adversarial_inputs_stay_fast`](tests/test_redaction.py) |
| Shell output and background-task notices in Claude Code logs aren't stored as your requests. | [`test_shell_output_and_notices_are_not_stored_as_requests`](tests/test_package.py) |
| Reading never writes, so it works in an assistant's sandbox, and `smem sql` can't write, attach or copy files. | [`test_sql_can_only_read`](tests/test_package.py), [`test_end_to_end_setup_read_only_use_and_uninstall`](tests/test_package.py) |
| Excluded folders match exactly (not similarly named folders), apply to sessions already stored, and are erased from the file, not just hidden. | [`test_exclusions_are_exact_complete_and_erased`](tests/test_package.py) |
| `smem forget` erases a session for good, only after a yes, and updates don't bring it back. | [`test_forget_erases_for_good_and_can_be_undone`](tests/test_package.py) |
| An unplugged drive is never taken for deletion: nothing is removed, and health raises an alarm. | [`test_unplugged_drive_is_not_taken_for_deletion`](tests/test_package.py), [`test_full_rebuild_failure_keeps_store`](tests/test_adapters.py) |
| Deleted logs are handled as `retention.keep` says, and health warns when an app's cleanup rule seems to change. | [`test_deleted_logs_are_kept_as_settings_say`](tests/test_package.py), [`test_deletions_are_sorted_alerted_and_followed`](tests/test_package.py) |
| Uninstall asks first, removes only what smem created, and never touches your history. | [`test_uninstall_keeps_files_it_did_not_create`](tests/test_package.py), [`test_skills_install_remove_and_never_touch_others`](tests/test_package.py), [`test_end_to_end_setup_read_only_use_and_uninstall`](tests/test_package.py) |
| The background job runs as you (never as an administrator), pins the folders it uses, and does nothing unless refresh is `auto`. | [`test_background_job_files_and_commands`](tests/test_package.py), [`test_background_job_pins_every_folder_and_escapes_paths`](tests/test_package.py), [`test_refresh_command_and_scheduled_runs_respect_the_mode`](tests/test_package.py) |
| Claude Code may run only the reading commands and a plain `smem update` without asking; anything that changes your setup asks. | [`test_skills_install_remove_and_never_touch_others`](tests/test_package.py) |

## What it protects against, and what it doesn't

- **Protects against** other accounts on the same computer reading your history through the memory: its files are readable only by you.
- **Protects against** common secret formats being stored: they're redacted first. This is best effort, and a secret in an unusual format can get through.
- **Protects against** smem changing your assistants' history or deleting anything it didn't create.
- **Doesn't protect against** software running as your own account. Anything that can read your files can read the memory, just as it can read the logs themselves.
- **Doesn't protect against** what assistants do with results: text they read from the memory goes to their provider like anything else they read, and old session text could contain instructions. The skill tells assistants that results are past records, but it can't guarantee an assistant ignores them.

## Reporting a problem

Please open an issue at https://github.com/Palis8d/session-memory/issues. For anything sensitive, say so in a short issue without the details, and we'll arrange a private channel.
