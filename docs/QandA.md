# HC-CDSS: In-Depth Q&A

> All patient names, clinical data, diagnoses, and session information referenced below are entirely fictitious, created solely for system testing and demonstration.

---

## Architecture and Design

**Q: Why use multiple specialized agents instead of one large agent?**

Each agent has a single, well-defined responsibility: fetch FHIR data, generate a diagnosis, look up protocols, check drug interactions, synthesize the result, or write the audit trail. This separation means you can swap out any individual agent without touching the others. If the RxNorm API changes its interface, you update only the Drug Interaction Agent. If you want to replace Vertex AI Search with a different retrieval system, only the Protocol Lookup Agent changes. It also means each agent can be independently scaled; if diagnosis inference becomes a bottleneck, you can run multiple diagnosis agent instances consuming from the same Pub/Sub subscription.

---

**Q: Why Pub/Sub between every agent rather than direct function calls?**

Three reasons. First, decoupling: agents don't need to know about each other's existence, only about the topics they publish to and the subscriptions they pull from. Second, replay: if the Orchestrator crashes mid-run, you can restart it and it will re-pull the messages from its Pub/Sub subscriptions without any agent needing to re-execute. Third, observability: every message flowing through the system is timestamped and logged in Pub/Sub, giving you a complete record of what happened and when for any given session.

---

**Q: How does session isolation work? How do you make sure Agent B doesn't pick up messages from a different patient's run?**

Every message published to Pub/Sub includes a `session_id` field in its attributes. Each agent filters pulled messages against the current session ID and ignores (NAKs) messages that don't match. This is why a `gcloud pubsub subscriptions seek` is run before each demo; it resets the subscription offset so stale messages from previous runs don't interfere.

---

**Q: Why does Step 2 run diagnosis and protocol lookup in parallel?**

They're independent operations. The Diagnosis Agent needs the patient context (from Step 1) but doesn't need protocol results. The Protocol Lookup Agent similarly only needs the patient context to build its queries. Running them in parallel saves roughly 10-15 seconds per pipeline execution. The Orchestrator pulls both results before synthesizing and waits for whichever takes longer.

---

**Q: The Orchestrator pulls from three Pub/Sub subscriptions. What happens if one of them is slow or never arrives?**

The current implementation has a pull timeout on each subscription. If a subscription doesn't return a message within the timeout window, the Orchestrator proceeds with whatever it has. For the protocol subscription specifically, 0 protocols is a valid result (4 of 10 patients returned 0 matches); the Orchestrator handles this gracefully and notes in the summary that no protocols were found.

---

**Q: How does the ADK framework relate to the Pub/Sub pipeline? Are they doing the same job?**

No, they operate at different levels. ADK is the conversational orchestration layer: it manages the session, routes user input to the root agent, tracks tool calls, and renders the trace view in the Dev UI. The Pub/Sub pipeline is the data flow layer: it moves structured clinical payloads between the specialist agents. ADK tools are what the root agent calls (`fetch_patient_context`, `run_diagnosis_agent`, etc.), and each tool wraps a Pub/Sub publish-then-wait operation. ADK doesn't know anything about FHIR or DLP; Pub/Sub doesn't know anything about conversational sessions.

---

**Q: Why `gemini-2.5-flash` specifically? Why not a more capable model?**

Speed and cost. Clinical decision support at this stage is more about reasoning quality per token than raw capability. Gemini 2.5 Flash generates coherent differential diagnoses and well-structured clinical summaries in 5-8 seconds. A more capable model would increase latency significantly for marginal quality gain on structured medical reasoning tasks. The model is pinned to `gemini-2.5-flash` rather than an unversioned or older alias; always pin versions in production to avoid breaking changes when Google updates the alias.

---

## FHIR and Data

**Q: What does the FHIR `$everything` operation actually return?**

`$everything` is a FHIR operation that returns all resources associated with a Patient: conditions, medications, allergies, observations, encounters, procedures, and more, in a single Bundle response. For the synthetic patients in this system, responses range from roughly 7,000 to 8,400 bytes. The Patient Context Agent parses the Bundle and extracts only the clinically relevant resource types: Condition, MedicationRequest, AllergyIntolerance, and Observation (labs and vitals).

---

**Q: Why do some patients have 0 labs/vitals in the context output?**

The synthetic patient FHIR bundles were built with conditions, medications, and allergies but not all patients were given Observation resources for vitals and labs. In a real deployment, these would come from the EHR. The pipeline handles missing resource types gracefully; the Patient Context Agent reports `labs_found: 0` and `vitals_found: 0` rather than failing. The Diagnosis Agent still generates clinically reasonable differentials based on the available conditions and chief complaint.

---

**Q: Why does the FHIR ID for Charlotte Blandy still say `patient-priya-patel`?**

FHIR resource IDs are immutable once created in the store. When the patient name was updated from Priya Patel to Charlotte Blandy, the change was made to the display name inside the Patient resource JSON, but the resource ID (`patient-priya-patel`) cannot be changed without deleting and re-creating the resource. In practice this is a non-issue: the FHIR ID is an internal identifier, and all clinical data, summaries, and audit records correctly reflect the name Charlotte Blandy.

---

**Q: How are the synthetic patients structured? What FHIR resource types does each bundle contain?**

Each patient bundle is a FHIR R4 `transaction` Bundle containing: one `Patient` resource (demographics, identifiers), one or more `Condition` resources (diagnoses with ICD-10 codes and clinical status), one or more `MedicationRequest` resources (medications with RxNorm codes and dosing), one `AllergyIntolerance` resource, and two to six `Observation` resources (labs and vitals where applicable). Each bundle is loaded via a single POST to the FHIR store's Bundle endpoint.

---

## DLP and Privacy

**Q: What exactly does "pseudonymization" mean in this context? Is the PHI gone or just hidden?**

Pseudonymization replaces PHI tokens with deterministic surrogate values encrypted using the KMS key. The original value is not stored, but given the same KMS key, you could reconstruct the original from the surrogate if needed (unlike anonymization, which is irreversible). For demo purposes the BEFORE/AFTER text looks the same in the console output because the transformation is applied to the internal representation of the summary before it's written to Firestore; the surrogate values are KMS-encrypted tokens, not readable names.

---

**Q: Why three DLP Moments? Why not just apply DLP once at the very end?**

Defense in depth. Moment 1 ensures no raw PHI ever travels on the Pub/Sub bus; if a message is replayed or inspected mid-pipeline, it's already scrubbed. Moment 2 catches names that Gemini might hallucinate into the diagnosis output based on patient data it was given. Moment 3 is the compliance-grade write gate; nothing enters Firestore or BigQuery without passing through DLP. Applying only at the end means a data breach anywhere in the middle of the pipeline exposes raw PHI.

---

**Q: Why did Peter J Rolle trigger an `AGE` DLP type when none of the other patients did?**

Peter J Rolle is 6 years old. Cloud DLP classifies patient age as a PHI identifier specifically when it's associated with a minor, because age combined with other clinical data is sufficient to re-identify a child patient in ways that it isn't for an adult. The Diagnosis Agent's output for the febrile seizure case explicitly mentioned the patient's age (as it should, since febrile seizure management is entirely age-dependent), which triggered the `AGE` detection. This is correct behavior.

---

**Q: What's the difference between DLP Moment 2 and Moment 3? They both run on Gemini output.**

Moment 2 runs on the raw differential diagnosis payload from the Diagnosis Agent: just the diagnosis names, ICD codes, and supporting evidence text. Moment 3 runs on the full synthesized clinical summary produced by the Orchestrator, which is a longer narrative that includes references to patient presentation, medications, protocol recommendations, and action items. The Orchestrator summary is the document that actually gets stored permanently; Moment 2 only governs what travels on the Pub/Sub bus between agents.

---

## Vertex AI Search and Protocols

**Q: How does the Protocol Lookup Agent decide what to search for?**

It builds 3-4 distinct queries per patient from: (1) the chief complaint free-text string from the FHIR Condition narrative, (2) `management guidelines {condition_display} {ICD-10 code}` for each of the top 2-3 conditions, and (3) a combined symptom-driven query. For David Conrad (stroke), the queries were: "clinical protocol Sudden left-sided weakness, facial droop, and aphasia, last known well 90 minutes ago", "management guidelines Acute Ischemic Stroke I63.9", "management guidelines Hypertension I10", and "management guidelines Atrial Fibrillation I48.91". Query 2 matched the NSTEMI protocol document, returning 1 result.

---

**Q: Why do some patients get 0 protocol matches even though Vertex AI Search is working?**

The corpus only has 3 documents. Vertex AI Search correctly retrieves documents when there's a reasonable semantic match. Sofia Reyes (PE) returned 0 because there's no PE document; the sepsis, NSTEMI, and CKD documents don't overlap sufficiently with pulmonary embolism management. Peter J Rolle (febrile seizure) returned 0 for the same reason. This is expected and correct behavior; the solution is adding more protocol documents, not fixing the search engine.

---

**Q: Why is the global endpoint required for Vertex AI Search? What's the difference?**

Vertex AI Search (Discovery Engine) has two endpoint patterns: a regional endpoint (`{location}-discoveryengine.googleapis.com`) and a global endpoint (`discoveryengine.googleapis.com`). For most data store configurations, especially those created via the GCP console without explicit regional routing, the search engine is registered globally and the regional endpoint returns 404. The global endpoint always works. This is a known gotcha that isn't well-documented; the fix is a single line change in the client initialization.

---

## Audit and Compliance

**Q: What exactly gets written to BigQuery per session?**

Two tables are written. `cdss_audit.audit_events` receives one row per audit event (5 per session); each row includes `session_id`, `agent_name`, `event_type` (e.g. `PATIENT_CONTEXT_FETCHED`, `DIAGNOSIS_GENERATED`, `SUMMARY_WRITTEN`), `timestamp`, `status`, `duration_ms`, `dlp_transformations`, and `metadata` (JSONB). `cdss_audit.clinical_summaries` receives one row per session with the complete structured output: `session_id`, `patient_id`, `top_diagnosis`, `all_diagnoses`, `protocols_matched`, `alert_count`, `has_critical_alerts`, `clinical_summary_text`, `dlp_phi_types`, and `firestore_path`.

---

**Q: The audit agent says "5 events processed" for every single patient. Is that a coincidence?**

No, it's by design. There are exactly 5 audit event publishes in the pipeline: one in the Patient Context Agent, one in the Diagnosis Agent, one in the Protocol Lookup Agent, one in the Drug Interaction Agent, and one in the Orchestrator. Each agent publishes one `AuditEventMessage` to `audit-events` after completing its work. The Audit Agent then pulls and processes all 5. The 504 timeouts after the 5th event are the Pub/Sub queue returning empty, not a failure.

---

**Q: What does "5 events processed and 15 failed" mean in the early Diane Okafor run?**

That was before the `consecutive_timeouts` fix was applied. The audit agent was pulling in batches of 20. It successfully processed 5 real events, then hit the empty queue and received 504 Deadline Exceeded responses 15 more times before the batch of 20 completed. Each 504 was logged as a "failed" event. After the fix (stop after 3 consecutive timeouts), the agent correctly exits after the queue is drained.

---

## Known Issues and Fixes

**Q: What was causing the Gemini synthesis failures before the SDK migration?**

The Orchestrator was initialized with the `vertexai` SDK, which requires a different auth flow than the `google.genai` SDK. When `GOOGLE_GENAI_USE_VERTEXAI=true` is set, the `google.genai` SDK routes requests through Vertex AI using Application Default Credentials; this is the correct pattern for a service account deployment. The `vertexai` SDK initialization was conflicting with the ADK session management layer, causing synthesis calls to fail silently. Migrating to `google.genai` with `GOOGLE_GENAI_USE_VERTEXAI=true` resolved it completely.

---

**Q: The diagnosis agent previously showed a deprecation warning. Has that been fixed?**

Yes, as of the March 2026 code review. The diagnosis agent has been migrated from `vertexai.generative_models.GenerativeModel` to the `vertexai` SDK's `GenerativeModel` with `GenerationConfig`, which is consistent with current ADK patterns and no longer generates the deprecation warning. The Orchestrator continues to use the `google.genai.Client` pattern with `vertexai=True`; both are valid approaches for Vertex AI-backed inference.

---

**Q: Why does the `gcloud pubsub subscriptions seek` command need to be run before each pipeline execution?**

Pub/Sub subscriptions retain unacknowledged messages until they're either pulled-and-acked or the message retention window expires (default 7 days). If you run the pipeline for patient A and then immediately start a run for patient B, the subscriptions may still have messages from patient A's run that were published but not yet consumed. The `seek --time=$(current_time)` command fast-forwards the subscription offset to "now", effectively discarding all existing undelivered messages and giving the next run a clean slate.

---

**Q: Will the RxNorm fix require changes to all 10 patient FHIR bundles?**

Yes. The `MedicationRequest` resources in each synthetic patient bundle contain RxNorm `coding` entries with RxCUI values that don't resolve against the NLM API. The fix is to look up each medication's correct RxCUI from the NLM RxNorm browser and update the FHIR JSON, then re-load the affected bundles to the FHIR store (FHIR allows updating existing resources via PUT). Once corrected, the Drug Interaction Agent's RxNorm API calls should return valid interaction data. Charlotte Blandy (methylergonovine and anticoagulants) and Sofia Reyes (rivaroxaban and NSAIDs) are the most clinically interesting cases to validate first.

---

**Q: Peter J Rolle is 6 years old. The AAP febrile seizure guideline covers 6 months to 5 years. Is he outside the guideline age range?**

Technically yes — the AAP 2011 Clinical Practice Guideline defines simple febrile seizure in children aged 6 months to 60 months (5 years). Peter is 72 months (6 years) at presentation. In practice, febrile seizures do occur in children up to age 6 and the same clinical approach applies: the AAP guideline's core recommendations (no routine LP, no CT, no EEG for a first simple febrile seizure) are clinically appropriate for a 6-year-old with a first generalized febrile seizure. The ICD-10 code R56.00 (Simple febrile convulsions) has no age restriction. For the CDSS demo, this is an intentional edge case: the system should retrieve the febrile seizure protocol and apply it, noting that the patient is at the upper boundary of the canonical age range. If this were a real deployment, a pediatric neurologist would determine whether the presentation warrants further workup given the age.


## Operations

**Q: How long does a full pipeline run take end-to-end?**

Typically 55-65 seconds from FHIR fetch to audit flush. The breakdown is roughly: FHIR $everything (1-2s), Gemini diagnosis inference (6-8s), Vertex AI Search queries (1.5-2.5s, 3-4 queries), FHIR drug interaction queries (4-6s), Gemini orchestrator synthesis (3-4s), Firestore and BigQuery writes (1-2s), audit flush (8-12s including timeout detection). The audit flush tail is the largest variable; it depends on how quickly Pub/Sub drains and when the consecutive_timeout condition triggers.

---

**Q: Why is the ADK session stored in SQLite locally instead of a cloud database?**

ADK's session management is local-first by design; the `local_storage.py` service writes to `.adk/session.db` in the working directory. This is appropriate for development and demo use. In a production deployment on Cloud Run or GKE, you would configure ADK to use a persistent session backend (or accept that sessions are ephemeral per container instance). The clinical data itself (the CDSSummary) is always written to Firestore; the ADK session is only the conversational context, not the clinical record.

---

**Q: What would it take to deploy this to Cloud Run?**

The main changes needed are: (1) containerize the ADK web server with a Dockerfile using Python 3.14 and requirements, (2) remove the `sa-key.json` direct credential loading and replace with Workload Identity Federation or Secret Manager, (3) set the environment variables as Cloud Run environment variables or Secret Manager references, (4) configure the container to expose port 8000. The Pub/Sub, FHIR, BigQuery, and Firestore integrations are all already cloud-native and require no changes. Estimated effort: 1-2 days.

---

**Q: Can this run multiple patients simultaneously?**

Not currently. The pipeline is designed for sequential execution from the ADK Dev UI. The Pub/Sub session filtering (matching messages by `session_id`) would theoretically support concurrent runs, but the ADK root agent is single-threaded and processes one conversation at a time. Concurrent execution would require either multiple ADK instances (one per patient) or a batch processing layer that bypasses the ADK conversational interface entirely.

---

**Q: What's the status of the protocol corpus? Are all patients covered?**

As of the March 2026 update, the corpus is complete — 10 protocol documents covering all 10 synthetic patient scenarios. Sepsis (SSC 2021), NSTEMI (ACC/AHA 2022), CKD+Diabetes (KDIGO/ADA 2022), Pulmonary Embolism (ESC 2019), Acute Ischemic Stroke (AHA/ASA 2019), Diabetic Ketoacidosis (ADA), COPD Exacerbation (GOLD 2023), Postpartum Hemorrhage (ACOG 2017), Febrile Seizure (AAP 2011), and Acute Liver Failure (AASLD 2011). Load all 10 via `scripts/setup_vertex_search.py`. Sofia Reyes (PE) and Peter Rolle (febrile seizure) previously returned 0 protocol matches; they will now match correctly once the new documents are indexed.
