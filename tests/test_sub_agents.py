import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import unittest

from core.sub_agents import (AgentResult, AgentTask, ConflictError,
                             SwarmOrchestrator)


def make_run(output="done", edits=None):
    def run(task: AgentTask):
        proposed = dict(edits or {}) if task.role.name == "implementer" else {}
        return AgentResult(task_id=task.task_id, role=task.role.name,
                           output=output, proposed_edits=proposed)
    return run


class TestSwarm(unittest.TestCase):
    def test_two_scoped_roles_sequential(self):
        orch = SwarmOrchestrator(make_run(edits={"core/x.py": "new"}))
        tasks = orch.plan("add feature", "ctx " * 5000)
        self.assertEqual([t.role.name for t in tasks], ["implementer", "reviewer"])
        self.assertLessEqual(len(tasks[0].context_package), 40000)
        results = orch.execute(tasks)
        applied = orch.apply_writes(results, approve=lambda path: True)
        self.assertIn("core/x.py", applied)
        self.assertTrue(any("approval" in a for a in orch.audit))

    def test_writes_rejected_without_approval(self):
        orch = SwarmOrchestrator(make_run(edits={"core/x.py": "new"}))
        results = orch.execute(orch.plan("t", "c"))
        applied = orch.apply_writes(results, approve=lambda path: False)
        self.assertEqual(applied, [])

    def test_overlapping_edits_raise_conflict(self):
        orch = SwarmOrchestrator(None)
        r1 = AgentResult("1", "implementer", "", {"core/a.py": "x"})
        r2 = AgentResult("2", "reviewer", "", {"core/a.py": "y"})
        with self.assertRaises(ConflictError):
            orch._check_conflicts([r1, r2])

    def test_role_cap_refuses_swarm_growth(self):
        from core.sub_agents import IMPLEMENTER, REVIEWER
        orch = SwarmOrchestrator(make_run())
        with self.assertRaises(ValueError):
            orch.plan("t", "c", extra_roles=[IMPLEMENTER, REVIEWER])


if __name__ == "__main__":
    unittest.main()
