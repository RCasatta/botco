# Possible work

Ideas worth doing, not decided yet.

## Calibrate the editor on the owner's verdicts

**Why.** The editor (same model as the writer) checks facts well: it reads the
lab report and compares numbers. But it approves almost everything that is
correct. Up to Oct 11, 2026 the owner rejected 4 posts the editor had
approved (#4, #15, #21, #25). Its sense of what is worth posting is the
writer's, not the owner's.

**Idea.** Show the editor, and probably the writer too, where the owner
overruled an approval:

- Source: posts and replies the editor approved, which the owner then
  rejected, sent back or dropped, with the owner's words.
- The owner's words come from what they wrote in the task's topic. Every
  message there is already stored as a comment on the task, so a plain
  sentence ("nothing new, we said this Monday") is enough. `/reject` and
  `/revise` stay an optional shortcut, not the way to do it.
- A verdict stated in words is recorded today only if an agent records it
  with `on_behalf_of`. For calibration that is not needed: an owner's comment
  on a post the editor approved, followed by a revision, a drop or a
  rejection, is the signal. Comments that only ask a question are not.
- Render the latest ~10 overrules from the last 30 days as a section after
  the role and before the notes: the text as it was, what happened, and the
  owner's words. It changes only when the owner overrules something, so the
  prompt cache keeps working.
- A few lines in `editor.md`: that list is the owner's bar. A draft that
  resembles one of those posts in topic, angle or type of claim gets a
  revise, even when its facts check out. An approval means "the owner would
  publish this", not "this is correct".

**With it, perhaps.**
- An approval states the draft's weakest point and why it is still worth
  posting, so approving is not the default.
- `status` shows the editor's approve/revise rate and how many of its
  approvals the owner overruled, to see within a week whether this helps.

**Open question.** The owner prefers a natural flow: comments, not commands.
That suggests going further: have the agent woken by the owner's comment on
a task classify it (verdict, change request, question) and record it, rather
than relying on the owner to use a command.
