# Persona setup

Tooling for setting up user personas across pilots. Each persona type lives in
its own subfolder with its own `config.json` + README, following the same
`config.json.example` convention as the rest of this repo.

| Persona | What it does |
|---------|--------------|
| [`HER/`](HER/README.md) | Sets up a HER (Home Energy Report) persona for a user: syncs the pilot's HER config, renders the DB SQL, uploads the interactions payload to S3, pushes NBI string resources, and runs the SHC 2.0 mock pipeline that backs the report's neighbourhood-comparison insights. |
| [`BestRateEmail/`](BestRateEmail/README.md) | Prepares and triggers a user-scoped Best Rate email in MasterPilot ProductQA: picks the highest-savings rate from the user's live Rate Comparison result, configures user-level email/NBI assets, writes and verifies the manual interaction, resets the sent count, and publishes the notification event. |
| [`TOUOnboarding/`](TOUOnboarding/README.md) | Prepares and triggers a user-scoped TOU Rate Onboarding email in MasterPilot ProductQA: reads the pilot's own ingestion contract, picks a valid TOU rate plan, builds and uploads the rate-change enrolment file, waits for the transition, and publishes/verifies the notification, auto-retrying the next ranked plan if one doesn't render. |
