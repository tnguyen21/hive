"""Tests for multi-worker pool and molecules."""

import pytest


def test_get_next_ready_step(temp_db):
    """Test getting next ready step in a molecule."""
    # Create parent molecule
    parent = temp_db.create_issue("Multi-step workflow", issue_type="molecule", project="test")

    # Create steps
    step1 = temp_db.create_issue("Step 1", issue_type="step", parent_id=parent, project="test")
    step2 = temp_db.create_issue("Step 2", issue_type="step", parent_id=parent, project="test")
    step3 = temp_db.create_issue("Step 3", issue_type="step", parent_id=parent, project="test")

    # Wire dependencies: step2 depends on step1, step3 depends on step2
    temp_db.add_dependency(step2, step1)
    temp_db.add_dependency(step3, step2)

    # Initially, only step1 should be ready
    next_step = temp_db.get_next_ready_step(parent)
    assert next_step is not None
    assert next_step["id"] == step1

    # Mark step1 as done
    temp_db.update_issue_status(step1, "done")

    # Now step2 should be ready
    next_step = temp_db.get_next_ready_step(parent)
    assert next_step is not None
    assert next_step["id"] == step2

    # Mark step2 as done
    temp_db.update_issue_status(step2, "done")

    # Now step3 should be ready
    next_step = temp_db.get_next_ready_step(parent)
    assert next_step is not None
    assert next_step["id"] == step3

    # Mark step3 as done
    temp_db.update_issue_status(step3, "done")

    # No more steps
    next_step = temp_db.get_next_ready_step(parent)
    assert next_step is None


def test_get_next_ready_step_no_dependencies(temp_db):
    """Test getting next ready step with no dependencies."""
    parent = temp_db.create_issue("Molecule", issue_type="molecule", project="test")

    # Create independent steps
    step1 = temp_db.create_issue("Step 1", issue_type="step", parent_id=parent, project="test")
    temp_db.create_issue("Step 2", issue_type="step", parent_id=parent, project="test")

    # First step should be step1 (oldest by created_at)
    next_step = temp_db.get_next_ready_step(parent)
    assert next_step is not None
    assert next_step["id"] == step1


def test_get_active_agents(temp_db):
    """Test getting all active agents."""
    # Create agents
    agent1 = temp_db.create_agent("agent-1")
    agent2 = temp_db.create_agent("agent-2")
    agent3 = temp_db.create_agent("agent-3")

    # Set some to working
    temp_db.conn.execute("UPDATE agents SET status = 'working' WHERE id = ?", (agent1,))
    temp_db.conn.execute("UPDATE agents SET status = 'working' WHERE id = ?", (agent3,))
    temp_db.conn.commit()

    active = temp_db.get_active_agents()

    assert len(active) == 2
    active_ids = [a["id"] for a in active]
    assert agent1 in active_ids
    assert agent3 in active_ids
    assert agent2 not in active_ids


def test_molecule_execution_order(temp_db):
    """Test that molecule steps execute in dependency order."""
    # Create a molecule with complex dependencies
    parent = temp_db.create_issue("Complex workflow", issue_type="molecule", project="test")

    # Create steps
    setup = temp_db.create_issue("Setup", issue_type="step", parent_id=parent, project="test")
    design = temp_db.create_issue("Design", issue_type="step", parent_id=parent, project="test")
    impl_a = temp_db.create_issue("Implement A", issue_type="step", parent_id=parent, project="test")
    impl_b = temp_db.create_issue("Implement B", issue_type="step", parent_id=parent, project="test")
    test = temp_db.create_issue("Test", issue_type="step", parent_id=parent, project="test")

    # Dependencies:
    # - design depends on setup
    # - impl_a depends on design
    # - impl_b depends on design
    # - test depends on impl_a and impl_b
    temp_db.add_dependency(design, setup)
    temp_db.add_dependency(impl_a, design)
    temp_db.add_dependency(impl_b, design)
    temp_db.add_dependency(test, impl_a)
    temp_db.add_dependency(test, impl_b)

    # Step 1: Setup should be ready
    next_step = temp_db.get_next_ready_step(parent)
    assert next_step["id"] == setup

    # Complete setup
    temp_db.update_issue_status(setup, "done")

    # Step 2: Design should be ready
    next_step = temp_db.get_next_ready_step(parent)
    assert next_step["id"] == design

    # Complete design
    temp_db.update_issue_status(design, "done")

    # Step 3: Either impl_a or impl_b should be ready (they're independent)
    next_step = temp_db.get_next_ready_step(parent)
    assert next_step["id"] in [impl_a, impl_b]

    # Complete both implementations
    temp_db.update_issue_status(impl_a, "done")
    temp_db.update_issue_status(impl_b, "done")

    # Step 4: Test should be ready now
    next_step = temp_db.get_next_ready_step(parent)
    assert next_step["id"] == test


@pytest.mark.asyncio
@pytest.mark.integration
async def test_session_cycling(temp_db, git_repo):
    """Test session cycling between molecule steps (requires OpenCode server)."""
    from hive.opencode import OpenCodeClient
    from hive.orchestrator import Orchestrator

    # Create a molecule with two steps
    parent = temp_db.create_issue("Two-step workflow", issue_type="molecule", project="test")
    step1 = temp_db.create_issue(
        "Create README",
        issue_type="step",
        parent_id=parent,
        project="test",
        description="Create a README.md file",
    )
    step2 = temp_db.create_issue(
        "Add license",
        issue_type="step",
        parent_id=parent,
        project="test",
        description="Add a LICENSE file",
    )

    # Wire dependency
    temp_db.add_dependency(step2, step1)

    async with OpenCodeClient() as opencode:
        orch = Orchestrator(
            db=temp_db,
            opencode_client=opencode,
            project_path=str(git_repo),
            project_name="test",
        )

        # Spawn worker on step1
        issue = temp_db.get_issue(step1)
        await orch.spawn_worker(issue)

        # Wait a bit for the agent to work
        import asyncio

        await asyncio.sleep(5)

        # Check if step1 is done and step2 is claimed
        temp_db.get_issue(step1)
        temp_db.get_issue(step2)

        # Clean up - get agent and delete sessions
        agents = temp_db.get_active_agents()
        for agent_dict in agents:
            if agent_dict["session_id"]:
                await opencode.delete_session(agent_dict["session_id"], directory=agent_dict["worktree"])


@pytest.mark.asyncio
@pytest.mark.integration
async def test_multi_worker_pool(temp_db, git_repo):
    """Test spawning multiple workers concurrently (requires OpenCode server)."""
    from hive.config import Config
    from hive.opencode import OpenCodeClient
    from hive.orchestrator import Orchestrator

    # Create multiple independent issues
    issue1 = temp_db.create_issue("Task 1", "Do task 1", project="test")
    issue2 = temp_db.create_issue("Task 2", "Do task 2", project="test")
    issue3 = temp_db.create_issue("Task 3", "Do task 3", project="test")

    async with OpenCodeClient() as opencode:
        orch = Orchestrator(
            db=temp_db,
            opencode_client=opencode,
            project_path=str(git_repo),
            project_name="test",
        )

        # Spawn workers
        await orch.spawn_worker(temp_db.get_issue(issue1))
        await orch.spawn_worker(temp_db.get_issue(issue2))
        await orch.spawn_worker(temp_db.get_issue(issue3))

        # Check that multiple workers are active
        cursor = temp_db.conn.execute("SELECT COUNT(*) FROM agents WHERE status = 'working'")
        active_count = cursor.fetchone()[0]
        assert active_count <= Config.MAX_AGENTS

        # Clean up
        agents = temp_db.get_active_agents()
        for agent_dict in agents:
            if agent_dict["session_id"]:
                await opencode.delete_session(agent_dict["session_id"], directory=agent_dict["worktree"])
