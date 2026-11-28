# Community telemetry privacy

Trip uploads default to the private logbook. To share a recording, choose community sharing explicitly. Older mobile recorder clients posting to `/api/trips/mobile-sync` without `confirm_community: true` still save their trips, and the logbook prompts the owner to confirm sharing before those trips influence public data. Direct GPX/JSON uploads via `/api/trips/upload` default to `private` and require selecting `community_anonymous` explicitly. Raw trip details, exports and off-network traces belong to the trip owner.

A public live segment grade requires three distinct, active contributors whose evidence was submitted during their current operator attestation. The calculation uses each eligible contributor's latest matched traversal on that segment. Terrain and weather grades remain available while there are fewer than three eligible contributors. Vehicle splits and roughness require three contributors in the relevant group. Travel speed and duration are rounded cohort statistics. The last-driven date is the UTC week containing the third most recent traversal. Off-network overlays show only approximate grid cells corroborated by three contributors.

Making a shared trip private or deleting it removes its observations and off-network candidates. TrailForge refreshes the affected stored grades, track grades and hotspots in the same transaction. Affected local packs need a rebuild before the server will serve them again. Previously downloaded copies, including packs already stored on a device, cannot be changed by the server.

## Attesting community contributors

Use the existing operator CLI to grant or revoke authority for a member's community evidence. Verify the member is an independent contributor through your existing knowledge of the community and attest at most one account per person. A grant keeps the member's role and access permissions. Registration, admission invites and account age grant no contributor authority.

```bash
python -m trailforge.auth.cli community-trust grant --email member@example.test
python -m trailforge.auth.cli community-trust revoke --email member@example.test
```

For a Compose installation, run the commands inside the API container. Use the API's PostgreSQL connection and privacy keyring. The cutoff uses the server's recorded trip, traversal and vote times. Older observations and votes remain in the account history. Rematching an older trip leaves it ineligible. Contributors still need to opt in to community sharing. Revocation or suspension removes the grant, refreshes affected grades, and invalidates their live packs in one transaction. A new grant applies to new evidence, including after reactivation. If a retained privacy key is missing during revocation, the API falls back to terrain and weather and expires live packs. Restore the keys, recompute grades and rebuild affected packs.

The offline demo seeder explicitly attests its known synthetic member accounts with unusable passwords. Its fixture data demonstrates live grades on a disposable instance.

## Upgrading an existing instance

1. Back up PostGIS and retain the privacy secret used for existing contributions. If it has already changed, set `TRAILFORGE_PREVIOUS_PRIVACY_SECRETS` to a JSON array containing the retained keys before the Alembic upgrade. Pass the same values to the API, worker and migration job.
2. Drain the old API, grading workers and pack publishers. Keep them stopped while applying the migration so earlier code cannot write old-policy grades, votes or pack files. Revision `0010_community_telemetry` discovers available withdrawal keys and pauses earlier trips pending owner confirmation. Revision `0013_community_contributor_trust` clears earlier live grades and reopens reports previously cleared by votes. Existing members start without an attestation.
3. Start the upgraded API and workers, then rebuild regional packs. The server withholds older manifests and safety snapshots. Standalone terrain archives and the baseline/weather global vector archive use independent data. Rebuild the vector archive if the deployment still has an older telemetry-bearing version.
4. For external hosting, build with `python -m trailforge.packs.cli --region REGION --static-only` or configure `PACKS_PUBLIC_BASE_URL` before building. The R2 sync uploads new static packs under `REGION/static_v2/` and the current global vector at `trailforge_vector_baseline_weather_v1.pmtiles`. Retire older objects and purge CDN caches for their previous direct URLs, including alternate object-store hostnames and static mirrors. Verify the new catalogue and manifests and check that retired URLs no longer serve the old data.
5. Ask users to remove and re-download older device packs before relying on pack grades or hazard reports. The server cannot recall downloaded bundles and disconnected copies. New packs keep reported warnings and apply durable operator moderation. The online map additionally reflects live, revocable vote outcomes.

## Rotating the privacy secret

Trip rows retain a nonce and a key fingerprint. The server-held privacy key is required to associate a trip with its observations. Keep the database and the deployment's keyring in separate protected storage, and avoid logging database bind parameters.

1. Place the active key in `TRAILFORGE_PREVIOUS_PRIVACY_SECRETS` and set `TRAILFORGE_PRIVACY_SECRET` to the new key. Supply both settings to the API and worker.
2. Run `python -m trailforge.worker.community_keys check` against PostGIS with the same settings. Keep the API and worker on the same configuration during the rotation. Withdrawals that require an unavailable key return a service error until the key is restored.
3. With contribution writers stopped, run `python -m trailforge.worker.community_keys rekey`, then restart writers with the new key. The command migrates linked observations and off-network candidates together. It also recovers pending legacy links when you supply a retained key after the migration. Run the check again with the retired keys removed before resuming traffic.

When no retained key matches a legacy contribution, those rows remain outside public grades and off-network overlays. The owner can confirm sharing again using the saved trimmed trip. Recovering the old rows for owner withdrawal still requires the original key and trip nonce. Run recovery before changing the visibility or deleting an unlinked legacy trip if you still need to recover its old rows.

## Hazard votes

Vote-derived resolution requires at least three eligible resolve votes, and those votes must outnumber eligible confirmations. Eligible confirmations can refresh a report. Ordinary votes still appear in the report's tally.

The report API's `status` field is the stored operator state. Its `effective_status` field includes natural decay and trusted votes. Clients that show the current warning should read `effective_status`. Offline snapshots use natural decay and the stored operator state so a revocable vote cannot remove a warning from a file.
