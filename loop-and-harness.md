# Loop & Harness Design

> Project: Semantic Video Replication Workflow  
> Positioning: A controllable AI production system for semantic replication of e-commerce short videos.  
> Goal: Turn an unstable multi-model generation process into a staged, reviewable, recoverable, and continuously improvable AI workflow.

---

## 1. Why This Project Needs Loop and Harness

This project is not a simple video generation tool.

Its core task is to take an existing high-performing e-commerce video, understand its semantic structure, and regenerate an equivalent video for a new product. This involves several unstable AI steps:

- Understanding the original video's rhythm, shot logic, visual language, and selling structure.
- Understanding the new product's appearance, selling points, usage context, and constraints.
- Rewriting the original video structure into a new product script.
- Converting the script into shot-level image and video prompts.
- Generating keyframes and video clips through external generation models.
- Reviewing and composing the generated clips into a final deliverable.

Each step can fail independently. A single failed shot can break the final video. A single hallucinated product detail can make the output commercially unusable.

Therefore, the system needs two design layers:

1. **Loop**: a feedback mechanism that allows the system and the user to review, revise, and regenerate intermediate outputs.
2. **Harness**: a control mechanism that constrains inputs, prompts, model behavior, review decisions, retries, fallbacks, and delivery quality.

In this project, Loop is responsible for continuous improvement. Harness is responsible for controllability.

---

## 2. Core Production Loop

The project-level loop is:

```text
Original video + new product materials
↓
Stage 1: Product and original video understanding
↓
Human confirmation of Product Brief when necessary
↓
Stage 2: Script replication and rewriting
↓
Stage 3: Shot-level prompt conversion
↓
Stage 3.5: Keyframe generation
↓
Human review of keyframes
↓
Stage 4: Shot-level video generation
↓
Stage 5: Composition, subtitles, OST, and final video assembly
↓
Final review
↓
User feedback / failure analysis
↓
Update script rules, prompt rules, model routing, retry policy, and review criteria
```

The most important product principle is:

> The final video should not be the first point where quality is judged. Quality should be judged at every intermediate layer: brief, script, prompt, keyframe, video clip, and composition.

This is the difference between a demo workflow and a production-grade AI system.

---

## 3. Three Levels of Loop Design

### 3.1 Project-level Loop

The project-level loop controls the full lifecycle of a replication task.

| Step | Input | Output | Review Point | Possible Next Action |
|---|---|---|---|---|
| Project creation | Original video, product image, listing URL | Project record | Input validation | Accept / reject / ask for more input |
| Product brief | Product image, listing, product video | Product Brief | Human confirmation | Confirm / revise / add missing info |
| Script replication | Original shot structure + Product Brief | New product script | Script quality check | Approve / regenerate / edit |
| Prompt conversion | Script | Shot-level prompts | Prompt guard | Approve / repair / regenerate |
| Keyframe generation | Image prompt + product reference | First/end keyframes | Human keyframe review | Approve / regenerate / change reference |
| Video generation | Video prompt + keyframes | Shot clips | Video guard | Approve / retry / route to fallback model |
| Composition | Approved clips + OST/subtitle plan | Final video | Final QA | Deliver / revise selected shots |

Project-level Loop is mainly about status management, human decision points, and overall progress visibility.

### 3.2 Shot-level Loop

The shot-level loop is the most important loop in this system.

A final video is composed of multiple shots. Most failures happen at the shot level, not the whole-video level. Therefore, each shot should be treated as an independently reviewable production unit.

```text
Shot script
↓
Shot prompt
↓
Keyframe
↓
Generated clip
↓
Clip review
↓
Approved: enter composition
Rejected: return to prompt / keyframe / model routing / product reference
```

Each shot should have its own status, error reason, review result, retry count, model choice, and final approval state.

Recommended shot states:

```text
PENDING
PROMPT_READY
PROMPT_REJECTED
KEYFRAME_GENERATING
KEYFRAME_REVIEW
KEYFRAME_APPROVED
KEYFRAME_REJECTED
VIDEO_GENERATING
VIDEO_APPROVED
VIDEO_REJECTED
COMPOSITION_READY
FAILED
```

This makes the system easier to debug and easier to operate.

### 3.3 Strategy-level Loop

The strategy-level loop converts production failures into reusable system knowledge.

```text
Failure case
↓
Failure classification
↓
Root cause analysis
↓
Rule update
↓
Prompt template update
↓
Model routing update
↓
Review criteria update
↓
Better result in future tasks
```

Examples:

| Failure | Root Cause | System Update |
|---|---|---|
| Product shape changes during generation | Prompt lacks strict product consistency rule | Add product consistency constraints to prompt guard |
| Static product shot gains random particles or smoke | Model over-adds cinematic effects | Add negative constraints for static product shots |
| One shot fails but the whole job stops | Workflow has weak partial failure handling | Add shot-level retry and fallback policy |
| Final video rhythm feels wrong | Generated clips do not match target duration | Add rhythm score and clip editing policy |
| Human reviewer repeatedly rejects the same type of shot | Review feedback is not converted into system rules | Add failure taxonomy and rule memory |

This is where the project evolves from a workflow into a learning production system.

---

## 4. Harness Design

Harness is the control layer around the AI workflow.

It does not mean one single component. In this project, Harness should be distributed across input validation, stage transition, prompt control, visual review, model routing, retry policy, human approval, logging, and metrics.

### 4.1 Input Harness

Input Harness decides whether a project is valid enough to start.

It should check:

- Is the original video URL accessible?
- Is the product image URL accessible?
- Is the video duration within the supported range?
- Is the product image clear enough?
- Is the product category supported?
- Is the listing URL available?
- Are there enough product materials to generate a reliable Product Brief?
- Are required API keys and storage services available?

Suggested output:

```json
{
  "passed": true,
  "blocking_issues": [],
  "warnings": ["Product listing URL missing; brief confidence may be lower."],
  "required_user_action": null
}
```

Input Harness prevents bad input from becoming expensive downstream failures.

### 4.2 Stage Transition Harness

Stage Transition Harness controls whether the workflow is allowed to move from one stage to the next.

Example rules:

- Stage 1 cannot enter Stage 2 unless Product Brief is finalized or simple mode explicitly bypasses it.
- Stage 2 cannot enter Stage 3 unless every required shot has a script.
- Stage 3 cannot enter Stage 3.5 unless prompts pass prompt validation.
- Stage 3.5 cannot enter Stage 4 unless keyframes are approved.
- Stage 4 cannot enter Stage 5 unless required clips are approved or explicitly marked skippable.

Suggested transition decision:

```json
{
  "from_stage": "KEYFRAME_REVIEW",
  "to_stage": "GENERATING",
  "allowed": false,
  "reason": "Shot 3 keyframe is still pending human review.",
  "required_action": "Approve or regenerate Shot 3 keyframe."
}
```

This prevents hidden workflow corruption.

### 4.3 Prompt Harness

Prompt Harness controls what the model is allowed to generate.

It should check:

- Does the prompt contain the product identity?
- Does it preserve the original shot's semantic structure?
- Does it include camera movement, action, subject, scene, and duration?
- Does it include negative constraints?
- Does it avoid unsupported model parameters?
- Does it avoid unsafe, misleading, or physically impossible claims?
- Does it avoid copying the original product or original brand too closely?

Recommended prompt scoring:

| Dimension | Description | Score |
|---|---|---|
| Product consistency | Product appearance and usage remain stable | 0-5 |
| Original structure inheritance | Shot function remains equivalent to original video | 0-5 |
| Generation feasibility | Prompt can realistically be generated by selected model | 0-5 |
| Commercial clarity | Selling point is clear and visible | 0-5 |
| Risk control | No hallucinated claims or unsafe elements | 0-5 |

A prompt should not go into generation if it fails basic feasibility and risk checks.

### 4.4 Visual Harness

Visual Harness evaluates generated keyframes and clips.

It should check:

- Does the product still look like the target product?
- Are color, shape, logo, material, and accessories consistent?
- Is the product usage physically plausible?
- Are hands, faces, and object interactions acceptable?
- Does the shot match its intended commercial function?
- Does the shot preserve the original video's rhythm and composition logic?
- Are there unwanted visual artifacts?

Suggested keyframe review object:

```json
{
  "shot_number": 2,
  "passed": false,
  "scores": {
    "product_consistency": 2,
    "composition": 4,
    "commercial_clarity": 3,
    "artifact_control": 2
  },
  "critical_issues": ["Product shape changed", "Extra component appeared"],
  "suggested_action": "regenerate_keyframe_with_stricter_product_reference"
}
```

Visual Harness is the main difference between a casual AI demo and a production workflow.

### 4.5 Model Routing Harness

The project already uses different generation platforms. This should become a formal model routing layer.

Suggested routing logic:

| Shot Type | Preferred Model | Fallback | Reason |
|---|---|---|---|
| Static product display | Seedance | Wan | Efficient and visually stable |
| First/end-frame anchored shot | Kling | Seedance | Better for strict frame anchoring |
| Complex hand interaction | Kling | manual review before retry | Higher risk of deformation |
| Low-cost preview | Seedance | none | Fast iteration |
| High-quality final delivery | Kling / Seedance based on shot type | Wan | Quality-oriented route |

Routing should consider:

- Shot type
- Motion complexity
- Need for first/end-frame control
- Cost limit
- Retry count
- Historical model success rate
- User-selected quality mode

### 4.6 Retry and Fallback Harness

Retry should not mean blindly running the same failed prompt again.

Recommended retry policy:

```text
First failure: retry with the same model after prompt repair
Second failure: switch reference image or keyframe strategy
Third failure: route to fallback model
Fourth failure: mark for human intervention
```

Each retry should record:

- Retry count
- Previous model
- New model
- Failure type
- Prompt change
- Reference change
- Result

This turns retry from gambling into controlled recovery.

### 4.7 Human-in-the-loop Harness

Human review should not be a vague manual step. It should be structured.

Recommended human review fields:

| Field | Description |
|---|---|
| review_status | approved / rejected / needs_revision |
| rejection_reason | standardized failure type |
| reviewer_note | free-form comment |
| severity | blocking / non-blocking |
| suggested_action | regenerate prompt / regenerate keyframe / switch model / accept with warning |
| reviewed_at | timestamp |
| reviewer | user or operator |

Human review should feed back into the strategy-level loop.

---

## 5. Failure Taxonomy

The system should standardize failure reasons.

Recommended failure types:

```text
INPUT_INVALID
VIDEO_URL_INACCESSIBLE
PRODUCT_IMAGE_INVALID
PRODUCT_BRIEF_LOW_CONFIDENCE
PRODUCT_INFO_MISSING
VIDEO_ANALYSIS_FAILED
SCRIPT_DRIFT
SCRIPT_TOO_GENERIC
PROMPT_MISSING_REQUIRED_FIELD
PROMPT_UNSAFE_OR_UNSUPPORTED
PROMPT_TOO_ABSTRACT
KEYFRAME_COLLAPSE
PRODUCT_DEFORMED
PRODUCT_IDENTITY_LOST
EXTRA_OBJECT_APPEARED
HAND_OR_BODY_ARTIFACT
TEMPORAL_INCONSISTENCY
RHYTHM_MISMATCH
MODEL_TIMEOUT
MODEL_API_ERROR
MODEL_OUTPUT_INVALID
OSS_UPLOAD_FAILED
AIRTABLE_SYNC_FAILED
COMPOSITION_FAILED
FINAL_QA_FAILED
```

Each failure event should be stored as structured data:

```json
{
  "project_id": "rec_xxx",
  "shot_id": "shot_003",
  "stage": "stage4_video_generation",
  "failure_type": "PRODUCT_DEFORMED",
  "severity": "blocking",
  "model": "seedance",
  "retryable": true,
  "retry_count": 1,
  "suggested_action": "switch_to_kling_with_keyframe_anchor",
  "human_note": "The product shape changed after the first second."
}
```

This is important for production operations and for portfolio storytelling.

---

## 6. Quality Metrics

The system should move from binary pass/fail review to measurable quality scoring.

### 6.1 Shot-level Metrics

Each shot should receive scores from 0 to 5:

| Metric | Meaning |
|---|---|
| Product Consistency Score | Whether product identity remains stable |
| Structure Inheritance Score | Whether the original shot logic is preserved |
| Motion Quality Score | Whether action and camera movement are natural |
| Commercial Clarity Score | Whether selling point is clear |
| Artifact Control Score | Whether visual defects are acceptable |
| Rhythm Match Score | Whether shot duration and rhythm match target |

### 6.2 Project-level Metrics

Recommended final metrics:

| Metric | Meaning |
|---|---|
| Replication Score | How well the output inherits the original video's structure |
| Product Fit Score | How well the output fits the new product |
| Visual Stability Score | How stable the product and scene remain |
| Delivery Readiness Score | Whether the final video is commercially usable |
| Cost per Final Video | Total model/API/storage cost per approved output |
| Time to First Preview | Time from project creation to first preview |
| Time to Approved Final | Time from project creation to approved final video |
| Human Intervention Count | Number of required manual actions |
| Regeneration Rate | Percentage of shots that required regeneration |

These metrics make the system manageable as a product, not just runnable as code.

---

## 7. Recommended Architecture Upgrade

Current structure:

```text
agents/
workflows/
services/
prompts/
models/
scripts/
```

Recommended addition:

```text
harness/
├── input_guard.py
├── stage_guard.py
├── prompt_guard.py
├── visual_guard.py
├── model_router.py
├── retry_policy.py
├── review_policy.py
├── failure_taxonomy.py
└── metrics.py
```

### 7.1 input_guard.py

Responsibilities:

- Validate URLs.
- Check required fields.
- Check video duration and format.
- Check product image availability.
- Check API key readiness.
- Return blocking issues and warnings.

### 7.2 stage_guard.py

Responsibilities:

- Enforce stage transition rules.
- Prevent incomplete projects from moving forward.
- Validate required intermediate outputs.
- Return required user actions.

### 7.3 prompt_guard.py

Responsibilities:

- Validate prompt completeness.
- Add or enforce negative constraints.
- Detect unsupported prompt patterns.
- Score generation feasibility.
- Repair low-quality prompts before generation.

### 7.4 visual_guard.py

Responsibilities:

- Evaluate generated keyframes and clips.
- Detect product deformation and identity loss.
- Compare output with target product references.
- Produce pass/fail decisions and review reasons.

### 7.5 model_router.py

Responsibilities:

- Select generation model based on shot type and quality needs.
- Switch model after failure.
- Balance cost, quality, speed, and controllability.

### 7.6 retry_policy.py

Responsibilities:

- Define max retry count.
- Define when to retry, repair, reroute, or stop.
- Prevent infinite loops and wasted cost.

### 7.7 review_policy.py

Responsibilities:

- Define human approval rules.
- Define when human approval is mandatory.
- Standardize rejection reasons and revision actions.

### 7.8 metrics.py

Responsibilities:

- Track cost, time, pass rate, regeneration rate, and model success rate.
- Generate project-level and shot-level quality reports.

---

## 8. Production Readiness Gap

The project already has a strong prototype foundation. It has multi-stage orchestration, external model integration, human review points, Airtable-based status storage, and video composition.

However, to become production-grade, it still needs upgrades in the following areas.

### 8.1 Product Experience Gap

Current state:

- The workflow is mainly API-driven.
- Airtable acts as the human review interface.
- The operator needs to understand the workflow deeply.

Production-grade target:

- A user-facing dashboard should show project status, shot status, review tasks, failures, costs, and final outputs.
- Users should not need to operate Airtable manually.
- Human review should become a structured product interface.

Suggested milestone:

- Build a minimal web console for project creation, shot review, keyframe approval, and final video delivery.

### 8.2 Reliability Gap

Current state:

- The system depends on multiple external APIs.
- Partial failures can happen at many points.
- Some failure recovery is documented but not yet formalized as system policy.

Production-grade target:

- All external calls should have timeout, retry, fallback, and error classification.
- Every job should be resumable.
- A failed shot should not corrupt the whole project.
- Long-running tasks should be managed by a real queue system instead of only background tasks.

Suggested milestone:

- Introduce a job queue such as Celery, RQ, Dramatiq, or a managed queue.
- Add idempotent job execution and resumable stage execution.

### 8.3 Data and State Management Gap

Current state:

- Airtable is useful for prototyping and human review.
- It is not ideal as the only production database.

Production-grade target:

- Use a real database for core project, shot, asset, job, review, and failure records.
- Keep Airtable only as an optional review or operations layer if needed.

Suggested milestone:

- Add PostgreSQL or another production database.
- Keep schema migrations.
- Define stable project, shot, asset, job, review, and metric tables.

### 8.4 Quality Evaluation Gap

Current state:

- The project has review concepts and audit service concepts.
- Quality criteria are not yet fully productized into measurable metrics.

Production-grade target:

- Each stage should produce measurable quality scores.
- Each rejection should have a standardized failure reason.
- The system should be able to answer: why did this video fail, where did it fail, and what should be changed next?

Suggested milestone:

- Add a quality report for every project.
- Add shot-level scorecards.
- Add failure taxonomy and review policy.

### 8.5 Security and Compliance Gap

Current state:

- The project is a public prototype repository.
- It depends on many API keys and external media URLs.

Production-grade target:

- Secrets should be managed securely.
- User uploads should be validated and isolated.
- Generated assets should have controlled access.
- The system should avoid storing sensitive or copyrighted materials without clear policy.
- The product should include commercial usage disclaimers and content review rules.

Suggested milestone:

- Add authentication.
- Add project-level access control.
- Add upload validation.
- Add signed URL policy.
- Add content policy and compliance review checklist.

### 8.6 Observability Gap

Current state:

- Logs exist, but production operations need deeper visibility.

Production-grade target:

- Track every stage's duration, cost, model call, retry, and failure.
- Provide dashboards for success rate, average generation time, model failure rate, and cost per approved video.

Suggested milestone:

- Add structured logging.
- Add request IDs and project IDs to all logs.
- Add metrics collection.
- Add error monitoring.

### 8.7 Testing Gap

Current state:

- The project has a test file and CI-related files, but the production test strategy still needs to be expanded.

Production-grade target:

- Unit tests for guards, routing, retry policy, and schema validation.
- Integration tests for Airtable/OSS/model service mocks.
- Golden sample tests for prompt generation.
- End-to-end smoke tests with a small demo project.

Suggested milestone:

- Build a test matrix around the five stages.
- Add mock services for external model APIs.
- Add regression tests for previously failed cases.

---

## 9. Production Readiness Rating

Current estimated level:

```text
Prototype / Technical Demo: 70%
Internal Tool: 45%
Production SaaS / Commercial Product: 20-30%
AI PM Portfolio Project: 75%
```

Interpretation:

- As a prototype, the project is already strong because it demonstrates a real AI workflow with multiple stages and external model integration.
- As an internal tool, it still needs better stability, review interface, failure recovery, and data persistence.
- As a production SaaS, it still needs authentication, billing, user experience, observability, compliance, queue infrastructure, and database design.
- As an AI Product Manager portfolio project, it is already valuable, especially if the documentation clearly explains Loop, Harness, review design, failure taxonomy, and production roadmap.

The fastest path is not to immediately turn it into a SaaS product. The fastest path is to turn it into a credible production-oriented portfolio case.

---

## 10. Recommended Roadmap

### Phase 1: Make the Existing Workflow Explainable

Goal: Turn the project from "a complex repo" into "a clear AI production system".

Tasks:

- Add this `docs/loop-and-harness.md` file.
- Add `docs/production-readiness.md` if needed.
- Add architecture diagram.
- Add stage status diagram.
- Add failure taxonomy.
- Add README section: "Why this is a Loop-and-Harness system".

Expected result:

- The repo becomes readable for recruiters, AI PM interviewers, engineers, and collaborators.

### Phase 2: Make the Workflow Operable

Goal: Reduce manual chaos and make the workflow easier to run repeatedly.

Tasks:

- Add input validation.
- Add stage transition guard.
- Add shot-level status machine.
- Add standardized failure reasons.
- Add structured review fields.
- Add cost and duration tracking.

Expected result:

- The workflow can be used repeatedly without relying on personal memory.

### Phase 3: Make the Workflow Reliable

Goal: Make failure recoverable.

Tasks:

- Add formal retry policy.
- Add model router.
- Add resumable job execution.
- Add queue system.
- Add external service mocks for testing.
- Add integration tests.

Expected result:

- A failed model call no longer breaks the whole project.

### Phase 4: Make the Workflow Productized

Goal: Give users and operators a real product interface.

Tasks:

- Build a minimal web dashboard.
- Add project creation page.
- Add shot review page.
- Add keyframe approval page.
- Add final video delivery page.
- Add review history and failure report.

Expected result:

- The system becomes usable by people other than the creator.

### Phase 5: Make the Workflow Commercially Deployable

Goal: Prepare for real production users.

Tasks:

- Add authentication.
- Add user/project permissions.
- Add billing or usage quota.
- Add production database.
- Add asset access control.
- Add observability dashboard.
- Add compliance and copyright policy.

Expected result:

- The project moves from portfolio/internal tool to early-stage commercial product.

---

## 11. Portfolio Framing

Recommended portfolio title:

> Semantic Video Replication Workflow: A Loop-and-Harness System for Controllable AI E-commerce Video Production

Recommended Chinese title:

> 面向电商短视频的语义复刻工作流：一个可控 AI 视频生产系统的 Loop 与 Harness 设计

Recommended one-paragraph positioning:

> This project explores how to make AI video generation controllable in a real e-commerce production scenario. Instead of treating video generation as a one-shot prompt task, the system decomposes the process into product understanding, original video analysis, script replication, prompt conversion, keyframe generation, shot-level video generation, human review, and final composition. The core product design is a Loop-and-Harness architecture: Loop enables iterative improvement across brief, script, prompt, keyframe, and shot outputs; Harness controls input quality, stage transition, prompt feasibility, visual consistency, model routing, retry policy, human approval, and production metrics.

Recommended AI PM storytelling angle:

> The key challenge is not whether AI can generate a video. The key challenge is whether an AI system can generate commercially usable videos repeatedly, under cost, quality, time, and brand constraints. This project is my attempt to turn an unstable creative generation process into a controllable production workflow.

---

## 12. Next Best Improvements

If only five things can be done next, prioritize these:

1. Add structured failure taxonomy and failure records.
2. Add shot-level quality scorecards.
3. Add model routing and retry policy.
4. Add a minimal review dashboard instead of relying only on Airtable.
5. Add production-readiness metrics: cost, duration, retry count, model success rate, and human intervention count.

These five improvements would make the project much stronger as both a working system and an AI Product Manager portfolio project.
