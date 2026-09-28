For any engineering task that involves implementing, changing, debugging,
refactoring, testing, or reviewing code:

1. Before doing code analysis or edits, check whether the current working
   directory contains a `memory_bank/` directory, a `memory-bank/`
   directory, or both.
2. If either directory exists, use the `memory-bank-analyzer` agent to
   read and summarize those files. Tell the subagent which of the two
   directories exist. Pass only a brief relevance-only restatement of the
   user's task for filtering what to include in the summary. Make sure the
   subagent knows not to implement, diagnose, plan, or otherwise act on the
   task itself. Use the summary it returns as context, then proceed with
   the task.
3. If neither directory exists, note that briefly and proceed normally.

This rule applies across repositories.
