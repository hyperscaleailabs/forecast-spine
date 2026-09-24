# notebooks/

Two kinds of document, deliberately not merged.

| File | Audience | Question it answers |
| --- | --- | --- |
| [`exploration.ipynb`](exploration.ipynb) | a reviewer reading the submission | What was built, and why is it correct? |
| [`playbook_0_acquisition.ipynb`](playbook_0_acquisition.ipynb) | an operator | How do the feeds get here, and what breaks? |
| [`playbook_1_asof_join.ipynb`](playbook_1_asof_join.ipynb) | an operator | What was knowable at the cutoff, and how is that enforced? |
| [`playbook_2_pipeline.ipynb`](playbook_2_pipeline.ipynb) | an operator | Where did every row go, and does a rerun agree? |
| [`playbook_3_gates.ipynb`](playbook_3_gates.ipynb) | an operator | Should this run ship, and who is paged if not? |
| [`playbook_4_operations.ipynb`](playbook_4_operations.ipynb) | an operator | What gets rolled back at 3am, and who owns it? |

`exploration.ipynb` is the narrative walkthrough, organised around the
assignment's Part 0-4. The playbooks are operational: each one runs its stage,
states what breaks, and describes what running it in production would require.
They share evidence and share no purpose.

Every notebook builds a **scratch** warehouse under a temporary directory. None
of them opens `data/warehouse/forecast_spine.duckdb`, so they stay runnable
while a SQL client holds that file -- DuckDB is single-writer.

Regenerate any of them with:

```bash
uv run jupyter nbconvert --to notebook --execute --inplace notebooks/<file>.ipynb
```
