Ordinary execution is one command, the same in every harness:

```
hearting run capability-route compose --start --prompt-file <task> \
  [--shape direct|solo|staged|framed] [--graph <stage,…>] [--campaign-key <stream>]
```

`hearting` is on PATH. `hearting run <utility>` runs that harness utility from
`AGENT_HOME` when it is set (dev activation: that checkout), else from the
installed release. The slug comes from the task. The stream defaults to this
session's latest one in the artifact root, else this folder's only active one;
otherwise name it with `--campaign-key` (a folder name or close spelling joins
that active stream; a refusal lists the keys) or opt out with `--unassigned`.
The runtime prepares the cycle, picks each leg's harness from live usage within
the sealed candidates, starts frames and the owner, reuses exact attempts, and
closes the route and cycle at success or once idle.

Follow the receipt. `parent_next=end-turn`: a runtime carrier owns the attempt;
end the turn with no wait, poll or recap. `parent_next=bounded-wait`: run
`parent_next_command` once. An absent directive is not `end-turn`. Start saves
the receipt named by `receipt_file`; the post-tool hook republishes its new
`parent_next`, and when that hook is unavailable read the saved receipt. After a wake or a correction, run
`resume_command`; answer a BLOCKED owner with `correction_command
--message-file <file>`. At `needs-question`, compare the two frame briefs and
follow the receipt's `next_step`: `resume_command --interview <file>` registers
the question, the person answers it once in the native question surface, and
`resume_command --answers <file>` records the intent, releases the gate and
starts the owner.

Inside an owner, a stage is `python3 "$AGENT_HOME/utilities/stage-dispatch-fallback.py"
--node <node> --start`: route, slug, parent and harness come from the owner's
current route and environment, and a member of a sealed parallel group starts
its whole group in one batch. Dispatch depth 3 is forbidden.

Message another session only with `hearting run peer-steward prompt
<name-or-pane> --body-file <file>`; reply text addressed to it reaches nobody.
