# Goldfish standards

The written standard a goldfish reviewer applies, one section per mode.

Consumed by: `.claude/skills/goldfish/SKILL.md` (Phase 1 names the section) and the `goldfish` agent in `.claude/agents/goldfish.md`.
Last Updated: 2026-10-27

Adapted, with changes, from the document standards of Google's Elephant-Goldfish Model (`resources/document_standards/` in github.com/google/elephant-goldfish-model), licensed Apache-2.0; a copy of that licence is `../LICENSE-APACHE-2.0.txt`. The modes, the wording and the report shape are this workspace's.

Severity, for every mode:

- **BLOCKER**: following the artifact as written produces a wrong or harmful result.
- **HIGH**: a reader must guess something that changes the result.
- **MEDIUM**: a gap that costs time or invites a wrong guess, without changing the result on a careful read.
- **LOW**: wording, order or format that slows the reader.

## plan

The artifact is a plan or a design waiting for approval. It passes if a competent engineer who has never seen this workspace understands what is built, why, and what was rejected, and finds no unhandled risk.

Check, in order:

1. **Objective.** Can you say what is built and why from the artifact alone? If not, stop with one BLOCKER that says so.
2. **Root problem.** Does the plan fix the cause, or patch a symptom?
3. **Reuse.** For every new script, helper, module or store the plan proposes, search the tree for one that already does the job. A match is a finding, with its path.
4. **Simplicity.** Flag features, options, abstractions or caches built for a future that the plan does not show is coming.
5. **Alternatives.** The plan names at least one rejected approach and the reason it lost. A single path with no alternative is a HIGH.
6. **Risks.** Failure modes, security, private data and anything live that the change touches, each with its mitigation. An unmitigated one is a finding.
7. **Unbacked claims.** Every statement about what existing code, a tool or a service does must be checkable. Check the ones you can. A claim the code contradicts is a BLOCKER.
8. **Altitude.** High-level sections that sink into helper names and loop variables are a LOW.

## reader

The artifact is meant to be acted on by someone who was not in the room: a brief for a yard, a handoff for the next session, a report for the operator, a proposal for a customer. It passes if that reader can act on it correctly from the artifact alone.

If the task names an audience, read as that audience (for example, "the customer's tender evaluator, who has the tender and this document and nothing else").

Write the UNDERSTOOD line first, before any finding: the author compares it with what they meant, and a mismatch is the most valuable result this mode produces.

Check:

1. **Undefined terms.** Every name, acronym, system, file or label that is neither defined in the artifact nor linked. Check the rest of the artifact before you flag one.
2. **Assumed steps.** Instructions that assume setup, permissions or knowledge the artifact does not give.
3. **Vague mechanism.** "It is handled automatically", "as discussed", "the usual way", "see above" with nothing above.
4. **Missing ask.** What exactly the reader must do, decide or deliver, and by when. If you cannot say, that is a BLOCKER.
5. **Contradictions** between two parts of the artifact.

Do not flag a term every member of the stated audience knows.

## ready

The artifact is an execution plan or a brief that a fresh agent will carry out. It passes the Zero-Decision Rule: the executor produces the right change on the first attempt without making a design decision of its own.

Check:

1. **Decisions left open.** List every choice the executor would have to make: a schema, a file location, a name, an error policy, a dependency, a test or no test. Each one is a finding.
2. **Files.** Every file the artifact says exists, open it. Every file it will touch is marked create, modify or delete. A missing mark is one MEDIUM for the whole list, not one per file.
3. **Steps.** Each step is an action on a named thing ("add field X to Y in Z"), not a summary ("update the service").
4. **Done.** The artifact names how the executor proves the work is done: a command, an exit code, a count.
5. **Boundaries.** What the executor must not touch is stated.

## drift

The artifact is a document that describes the current system: a rule, a reference, an architecture page, a README. Report only where the document and the code disagree.

Check every claim the document makes about the present:

1. **Enforcement claims.** "Held by X", "X refuses Y", "a gate checks Z", "fails closed". Open X and confirm it does that today. A claim of enforcement the code does not perform is a BLOCKER.
2. **Names.** Every file, function, flag, command and config key the document names as existing. Confirm each one exists.
3. **Behaviour.** Defaults, limits, thresholds and formats the document states. Confirm each against the code.

A sentence that describes a plan, a proposal or the past is not drift. If every claim holds, return no findings.

## proposal

The artifact proposes something to start: an initiative, an investment, a product move, a partnership, a hire, a project. It passes the Heilmeier catechism, named after George Heilmeier, who used these questions to judge research proposals. Each question must be answered in the artifact itself, plainly, in words a smart outsider understands.

Check that the artifact answers each of these. A missing answer is a finding. An answer that is only an assertion, with no number, name, date or source behind it, is a finding too.

1. **Objective.** What is being attempted, with no jargon? If you cannot say it in two sentences after reading, that is a BLOCKER.
2. **Today.** How is this done today, by us or by others, and where does that fall short?
3. **What is new.** What is different in this approach, and why should it succeed where today's way does not?
4. **Who cares.** Who gains if it works, and what changes for them? Name them.
5. **Risks.** What can make it fail? What does failure cost, and what is the payoff if it works?
6. **Cost.** Money, people and time, in numbers.
7. **Timeline.** How long until a first result, and until the full result?
8. **Exams.** What is checked midway, and what final result proves success? Each must be a measurable result with a date. "Progress is reviewed" is not an exam.

Also check that the numbers agree with each other: a cost that does not fit the timeline, or a payoff larger than the market the artifact itself describes, is a HIGH.

## code

The blinded pass of `/canopus` step 6. The artifact is a diff, with read access to the tree. You are deliberately NOT given the plan or the reason for the change.

Check:

1. Defects visible from the diff and the tree alone: wrong logic, an unhandled error path, a broken caller, a guard that refuses nothing, a silent fallback.
2. Callers of every changed function. Use `codegraph_explore`, and confirm each caller still works.
3. Anything the diff makes reachable that was not reachable before: a write, a send, a subprocess, a network call.

Do not judge style. Do not guess at intent. If the change looks deliberate but wrong, report it.
