# Fix Log — ADK Healthcare CDSS

All bugs, clinical accuracy issues, and security gaps identified in the deep-dive
code review. Each fix is tagged and cross-referenced to the file it touches.

---

## Critical Fixes

### F-PHI — PHI stripped from Gemini diagnosis prompt
**File:** `agents/diagnosis/agent.py` — `_build_diagnosis_prompt()`  
**Issue:** The prompt sent to Gemini included raw `name`, `mrn`, and `dob` fields from
the patient snapshot. DLP only ran on the *output*, meaning patient identifiers were
transmitted to the Gemini inference endpoint before any pseudonymization.  
**Fix:** `name`, `mrn`, and `dob` are no longer extracted or included in the prompt.
The model receives only clinical data: age, gender, encounter reason, conditions,
medications, allergies, labs, and vitals. Patient identity is never sent to inference.

---

### F-SNAP — PatientSnapshot forwarded through drug interaction to orchestrator
**File:** `agents/drug_interaction/agent.py` — `run_drug_interaction_check()`  
**Issue:** `DrugInteractionMessage.patient_snapshot` was always `None`. The Orchestrator
checks this field to build `CDSSummary.patient_snapshot_pseudonymized`, so that field
was always missing from every Firestore record.  
**Fix:** Agent 4 now pulls a `PatientContextMessage` from
`sub_drug_interaction_patient_context`, validates the session ID matches, and attaches
the `PatientSnapshot` to the outgoing `DrugInteractionMessage`.

---

### F-DLP2 — DLPRedactionMoment (Moment 2) now populated in DiagnosisMessage
**File:** `agents/diagnosis/agent.py` — `run_diagnosis_agent()`  
**Issue:** The system claimed 3 DLP redaction demo moments, but Moment 2 was never
built. The diagnosis agent created a `DLPAuditRecord` but never constructed the
`DLPRedactionMoment` or attached it to `DiagnosisMessage.dlp_redaction_moment`. The
Orchestrator's redaction log only ever showed Moment 3.  
**Fix:** After DLP runs on the Gemini output, a `DLPRedactionMoment` is built with
`before_excerpt` (raw Gemini output) and `after_excerpt` (DLP-cleaned version), logged
to console in the same banner format as Moment 3, and attached to the `DiagnosisMessage`.

---

### F-VALIDATE — config.validate() called at startup
**Files:** `cdss_agent/agent.py`, `shared/config.py`  
**Issue:** `config.validate()` existed but was never called. A missing `GCP_PROJECT_ID`
would only fail when the first API call was made, producing a cryptic error deep in a
GCP SDK rather than a clear startup message.  
**Fix:** `config.validate()` is called at module import in `cdss_agent/agent.py`. Also
added a `location` check to `validate()` since DLP and FHIR both require it.

---

## High Priority Fixes

### F-AGE — Age calculation is now month/day aware
**File:** `agents/patient_context/agent.py` — `_parse_patient()`  
**Issue:** `age = datetime.utcnow().year - birth_year` ignored the month and day.
A patient born December 1990 queried in January 2026 would show age 36, not 35.  
**Fix:** Uses `date.fromisoformat(dob)` with a full birthday comparison:
```python
age = today.year - dob_date.year - (
    (today.month, today.day) < (dob_date.month, dob_date.day)
)
```
Falls back to year-only if the DOB string cannot be parsed as ISO date.

---

### F-TIMEOUT — Diagnosis and protocol pull timeouts raised from 10s to 45s
**File:** `agents/orchestrator/agent.py` — `run_orchestrator()`  
**Issue:** The Orchestrator pulled DrugInteractionMessage with a 60s timeout, then
pulled DiagnosisMessage and ProtocolMessage with only 10s each. Under load (or when
parallel agents are slow), diagnosis and protocol results were silently dropped and
synthesis proceeded without them.  
**Fix:** Both timeouts raised to 45s, giving parallel agents adequate time to publish
their results after the drug interaction message is received.

---

### F-K+ — Potassium alert threshold raised from 5.0 to 5.5 mEq/L
**File:** `agents/drug_interaction/agent.py` — `CONTRAINDICATION_RULES`  
**Issue:** The ACE inhibitor hyperkalemia alert triggered at K+ > 5.0 mEq/L. Clinical
consensus (ACC/AHA, KDIGO) uses 5.5 mEq/L as the action threshold. Alerting at 5.0
flags borderline-normal values and causes alert fatigue.  
**Fix:** Threshold raised to `>= 5.5 mEq/L`. Condition key renamed from
`potassium_above_5` to `potassium_above_5_5` for clarity.

---

### F-SULF — Sulfonamide/furosemide cross-reactivity downgraded to LOW
**File:** `agents/drug_interaction/agent.py` — `CROSS_REACTIVITY_RULES`  
**Issue:** Furosemide and hydrochlorothiazide were listed as MODERATE cross-reactivity
risk with sulfonamide allergy. Current evidence (ACAAI, AAD, 2022-2025) significantly
downgraded this risk — the structural difference between sulfonamide antibiotics and
non-antibiotic sulfonamides means clinically meaningful cross-reactivity is unlikely.
The MODERATE alert was causing alert fatigue in demos.  
**Fix:** Severity downgraded to `AlertSeverity.LOW`. Evidence note updated to reflect
current literature and recommend clinical assessment.

---

### F-LOINC — Added eGFR MDRD LOINC code 33914-3
**File:** `agents/drug_interaction/agent.py` — `_check_contraindications()`  
**Issue:** The eGFR lookup only checked LOINC codes `69405-9` (CKD-EPI) and `62238-1`
(CKD-EPI race-adjusted). LOINC `33914-3` (MDRD equation) is still widely reported by
older EHR systems and was silently skipped, causing metformin/glipizide contraindication
checks to miss patients with MDRD-based eGFR values.  
**Fix:** Added `"33914-3"` to the eGFR LOINC code set.

---

## Medium Priority Fixes

### F-DLP4 — DLP findings count standardized to `transformed_count`
**Files:** `agents/diagnosis/agent.py`, `agents/protocol_lookup/agent.py`,
`agents/orchestrator/agent.py`  
**Issue:** Different agents used different fields from `TransformationSummary` to count
DLP findings. `protocol_lookup` used `summary.transformed_count`; `diagnosis` and
`orchestrator` used `len(summary.results)`. These measure different things and made
audit metrics inconsistent across agents.  
**Fix:** All agents now use `summary.transformed_count` (total transformation operations
applied), which is the more direct and consistent measure.

---

### F-DEDUP — Protocol deduplication uses content hash when doc.id is empty
**File:** `agents/protocol_lookup/agent.py` — `_search_protocols()`  
**Issue:** `protocol_id = doc.id or str(uuid.uuid4())`. When `doc.id` is empty, a
random UUID was generated each time the same document appeared in results. The
`seen_ids` deduplication set never matched these, so the same protocol could appear
multiple times in the output.  
**Fix:** `stable_id = doc.id or hashlib.md5(title.encode()).hexdigest()`. A stable
content-based hash ensures the same document is correctly deduplicated across queries.

---

### F-BP — Blood pressure component dict serialized to string
**File:** `agents/patient_context/agent.py` — `_parse_observation()`  
**Issue:** Blood pressure panel observations stored `value=components` where `components`
is a dict (e.g. `{"Systolic BP": "120 mmHg", "Diastolic BP": "80 mmHg"}`). The
`VitalSign.value` field is typed `Any`, so this passes validation, but downstream prompt
builders calling `v.get('value')` receive a dict instead of a scalar string, which can
produce malformed prompt lines.  
**Fix:** Component dict is serialized to a JSON string before storage:
`value=json.dumps(components)`. Downstream code receives a readable string.

---

### F-ENC — First Encounter retained; subsequent encounters no longer overwrite it
**File:** `agents/patient_context/agent.py` — `parse_fhir_bundle()`  
**Issue:** Each `Encounter` resource overwrote `snapshot.encounter_id` and
`snapshot.encounter_reason`. FHIR `$everything` returns entries in reverse-chronological
order, so the most recent encounter would be overwritten by older ones.  
**Fix:** Encounter fields are only set if `snapshot.encounter_id is None`, retaining
the first (most recent) encounter parsed from the bundle.

---

### F-DLPID — patient_id excluded from DLP-processed payload
**File:** `agents/orchestrator/agent.py` — `run_orchestrator()`  
**Issue:** `summary_payload` included `"patient_id": patient_id`. If the patient ID
contains a name or date segment (e.g. `"patient-marcus-webb-1966"`), DLP might tokenize
it, replacing the session correlation key with `[PERSON_NAME]` and breaking downstream
lookups.  
**Fix:** `patient_id` removed from the payload passed through DLP. It is reassembled
into `CDSSummary` separately after DLP completes.

---

### F-AUDIT1 — Consecutive timeout limit raised from 2 to 3
**File:** `agents/audit/agent.py` — `process_audit_events()`  
**Issue:** The audit loop stopped after 2 consecutive Pub/Sub pull timeouts. A single
brief network blip causing 2 sequential failures would abandon the entire remaining
audit queue — a serious gap for HIPAA audit completeness.  
**Fix:** Limit raised to 3 consecutive timeouts before stopping.

---

### F-AUDIT2 — Extended PHI_FIELD_NAMES with additional HIPAA identifiers
**File:** `agents/audit/agent.py` — `PHI_FIELD_NAMES`  
**Issue:** The PHI guard set was missing several HIPAA Safe Harbor identifiers:
`encounter_id`, `insurance_id`, `account_number`, `npi`, `fax_number`, `ip_address`,
`device_id`, `biometric_id`.  
**Fix:** All 18 HIPAA Safe Harbor category field names added to the set.

---

## Summary Table

| Tag | Severity | File | Description |
|---|---|---|---|
| F-PHI | Critical | diagnosis/agent.py | Strip PHI from Gemini prompt |
| F-SNAP | Critical | drug_interaction/agent.py | Forward PatientSnapshot to Orchestrator |
| F-DLP2 | Critical | diagnosis/agent.py | Populate DLPRedactionMoment (Moment 2) |
| F-VALIDATE | Critical | cdss_agent/agent.py, shared/config.py | Call config.validate() at startup |
| F-AGE | High | patient_context/agent.py | Month/day-aware age calculation |
| F-TIMEOUT | High | orchestrator/agent.py | Raise parallel pull timeouts to 45s |
| F-K+ | High | drug_interaction/agent.py | Potassium threshold 5.0 → 5.5 mEq/L |
| F-SULF | High | drug_interaction/agent.py | Sulfonamide/furosemide alert LOW |
| F-LOINC | High | drug_interaction/agent.py | Add MDRD eGFR LOINC 33914-3 |
| F-DLP4 | Medium | diagnosis, protocol_lookup, orchestrator | Standardize DLP count field |
| F-DEDUP | Medium | protocol_lookup/agent.py | Stable hash for protocol deduplication |
| F-BP | Medium | patient_context/agent.py | Serialize BP component dict to string |
| F-ENC | Medium | patient_context/agent.py | Retain first (most recent) encounter only |
| F-DLPID | Medium | orchestrator/agent.py | Exclude patient_id from DLP payload |
| F-AUDIT1 | Medium | audit/agent.py | Consecutive timeout limit 2 → 3 |
| F-AUDIT2 | Medium | audit/agent.py | Extend PHI_FIELD_NAMES set |
