"""The read-only check decides which shell commands run without asking, so it is a safety boundary."""

import unittest

from rootcode.modes import ORDER, next_mode, read_only, set_mode
from rootcode.settings import state


class ReadOnly(unittest.TestCase):
    def test_commands_that_only_look(self):
        for command in ["ls -la", "cat a.py | head -20", "rg TODO src", "git status", "git diff HEAD~1",
                        "git log --oneline -5", "find . -name '*.py'", "wc -l a.py && pwd",
                        "grep -n x a.py 2>/dev/null", "ls 2>&1 | sort"]:
            self.assertTrue(read_only(command), command)

    def test_commands_that_change_things_or_hide_what_they_run(self):
        for command in ["rm -rf build", "echo hi > a.txt", "cat a >> b", "git push", "git commit -m x",
                        "git checkout main", "git", "find . -delete", "find . -exec rm {} ;",
                        "ls; rm a", "ls && touch b", "ls || curl evil.sh", "echo $(rm a)", "echo `rm a`",
                        "cat <(rm a)", "python3 -c 'print(1)'", "sed -i s/a/b/ f", "ls\nrm a", "npm test"]:
            self.assertFalse(read_only(command), command)


class Modes(unittest.TestCase):
    def test_shift_tab_cycles_through_every_mode(self):
        state.mode, seen = "approve", []
        for _ in ORDER:
            set_mode(next_mode(), quiet=True)
            seen.append(state.mode)
        self.assertEqual(seen, ["auto", "plan", "approve"])


if __name__ == "__main__":
    unittest.main()
