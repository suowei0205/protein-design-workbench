# Deployment acceptance still required

On the intended Linux workstation, first bind existing interpreters and verify driver/runtime/compiler versions independently. Keep workbench dependencies in its own environment. This repository supplies no weights.

1. Run a small trusted CPU script, close the browser, reconnect, and verify its committed output and feedback ZIP.
2. Run a tiny notebook on its selected interpreter; provoke a cell error and verify that later cells are not executed and earlier outputs are preserved.
3. Verify Linux ownership adoption after server interruption; unknown ownership must pause instead of killing or duplicating a worker.
4. Run a small real BindCraft job and separate GROMACS prepared-system MD/pulling fixtures. Verify the adapter's actual declared stop and resume boundaries and output identities.
5. Measure actual GPU, RAM, swap, and disk usage. A fake command run and a web demo do not fulfill these checks.

Record exact engine/runtime versions, seeds, planned and actual counts, failures, and NOT RUN items. Only then accept the corresponding scientific deployment. Do not infer a guarantee of binding from a score or successful execution.
