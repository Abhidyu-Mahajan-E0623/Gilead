# Implementation Plan

## Goal
- Stop answering from `resolution_and_response_to_rep`.
- Match the user question to the closest playbook scenario, then pass only that scenario's `what_happened` to the LLM.
- Return answers as bullet points starting with `•`.
- Handle full-match, partial-match, vague-match, and ambiguous-name queries predictably.
- Require identifier follow-ups before answering when the provider, account, or territory has not been uniquely identified.

## Current Behavior To Replace
- `src/responder.py` sends ranked matches with `resolution_and_response_to_rep` into the prompt.
- The fallback path also returns `resolution_and_response_to_rep` directly.
- The prompt currently asks the model to select exact sentences from `resolution_and_response_to_rep` and does not produce bullet points.

## Target Behavior

### Case 1: Full scenario match
- Use retrieval to find the best matching inquiry.
- If the user question closely resembles the matched inquiry's `field_rep_says`, treat it as a full-scenario question.
- Pass only the matched inquiry's `what_happened` to the LLM.
- Instruct the LLM to answer from that context in as many bullet points as possible.
- Allow the LLM to combine closely related facts into one bullet when they are clearer together.

### Case 2: Partial question
- If the question maps to the same scenario but asks for only one part of it, still pass only the matched inquiry's `what_happened`.
- Tell the LLM to answer only the part explicitly asked.
- Avoid unrelated facts from the same scenario.

### Case 3: Vague but matchable question
- If the question is too vague to support a specific answer but still matches a playbook scenario, pass only the matched inquiry's `what_happened`.
- Tell the LLM to give a general answer without exposing specific names, IDs, exact locations, or other identifying details.

### Ambiguous name / identifier follow-up
- Before calling the LLM, inspect the best-matched inquiry's `what_happened` for identifiers such as provider NCP ID (NPI in source data) or `HCO ID`.
- If the user message appears to mention only a provider or account name, or otherwise lacks enough issue context to disambiguate, ask a follow-up instead of answering.
- The follow-up should request the relevant identifier type already present in `what_happened`:
  - Ask for provider `NCP ID` when the matched context contains an `NPI`.
  - Ask for `HCO ID` when the matched context contains an `HCO ID`.
- If the rep refers to a territory-sensitive issue without naming the territory, ask for the territory ID before answering.
- Treat matching names in the demo JSON as non-unique by default, as they would be in a real-world assistant flow.
- When the user replies with a requested identifier, reuse the original question plus the validated identifier instead of treating the reply as a brand-new query.
- If the user provides an invalid or mismatched identifier, ask for the correct identifier instead of repeating the original follow-up text unchanged.

## Implementation Steps
1. Update `src/responder.py` context-building so the LLM payload includes `what_happened` instead of `resolution_and_response_to_rep`.
2. Add responder helpers to:
   - detect whether the best match should be treated as `full`, `partial`, or `vague`
   - detect ambiguous name-only queries and produce a follow-up request
  - normalize the final answer into lines that start with `•`
3. Replace the LLM prompt so it:
   - uses only the matched inquiry's `what_happened`
   - respects the computed question mode
   - answers in bullet points only
   - gives general, non-identifying output for vague queries
4. Rewrite the non-LLM fallback path to mirror the same behavior from `what_happened`, including bullet formatting, partial selection, and generalized output where possible.
5. Keep retrieval unchanged unless the new behavior shows a clear mismatch during validation.

## Validation Steps
1. Run `src/validate_demo.py` to confirm the app still loads the playbook and the responder still returns answers.
2. Run a few targeted responder checks for:
   - a full scenario phrasing
   - a partial question on one fact from a scenario
   - a vague question that should become general
   - a name-only question that should trigger NPI/HCO ID follow-up
3. If formatting is inconsistent, tighten post-processing so every visible answer line starts with `•`.

## Guardrails
- Do not send `resolution_and_response_to_rep` to the LLM.
- Do not expose specific identifiers in vague-mode answers.
- Do not answer an ambiguous name-only query when the matched scenario already contains an `NPI` or `HCO ID` that should be used to confirm identity.
