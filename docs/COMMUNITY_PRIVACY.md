# Community telemetry privacy

Trip uploads default to the private logbook. To share a recording, choose community sharing explicitly. Older recorder clients can save their trips, and the logbook prompts the owner to confirm sharing before those trips influence public data. Raw trip details, exports and off-network traces belong to the trip owner.

A public segment grade requires three distinct contributors. The calculation uses each contributor's latest matched traversal on that segment. Vehicle splits and roughness require three contributors in the relevant group. Travel speed and duration are rounded cohort statistics. The last-driven date is the UTC week containing the third most recent traversal. Off-network overlays show only approximate grid cells corroborated by three contributors.

Making a shared trip private or deleting it removes its observations and off-network candidates. TrailForge refreshes the affected stored grades, track grades and hotspots in the same transaction. Affected local packs need a rebuild before the server will serve them again. Previously downloaded copies, including packs already stored on a device, cannot be changed by the server.

## Upgrading an existing instance

1. Back up PostGIS and retain the privacy secret used for existing contributions. If it has already changed, set `TRAILFORGE_PREVIOUS_PRIVACY_SECRETS` to a JSON array containing the retained keys before the Alembic upgrade. Pass the same values to the API, worker and migration job.
2. Apply the migrations before serving traffic. Revision `0010_community_telemetry` discovers available withdrawal keys, pauses earlier community trips pending owner confirmation, clears earlier telemetry-based grades, and reapplies the stronger hazard voting rule. Owners can use **Confirm community sharing** to re-enable each trip. Their trip and raw export remain available during the pause.
3. Rebuild local vector archives and regional packs. Existing terrain-only archives remain available. For external hosting, use `python -m trailforge.packs.cli --region REGION --static-only` or configure `PACKS_PUBLIC_BASE_URL` before building. Static regional packs retain terrain grades, rainfall, reports and closures. The deployment can continue serving those files across trip withdrawals.
4. Upload rebuilt static artifacts to R2. The sync command publishes the global vector archive at `trailforge_vector_baseline_weather_v1.pmtiles` and static regional packs under `REGION/static_v1/`. Remove older telemetry-bearing objects and purge CDN caches for their former URLs, including any direct object-store hostnames. Apply the same replacement to independent static mirrors. A new API catalog entry cannot retract copies already served by an older origin.

## Rotating the privacy secret

Trip rows retain a nonce and a key fingerprint. The server-held privacy key is required to associate a trip with its observations. Keep the database and the deployment's keyring in separate protected storage, and avoid logging database bind parameters.

1. Place the active key in `TRAILFORGE_PREVIOUS_PRIVACY_SECRETS` and set `TRAILFORGE_PRIVACY_SECRET` to the new key. Supply both settings to the API and worker.
2. Run `python -m trailforge.worker.community_keys check` against PostGIS with the same settings. Keep the API and worker on the same configuration during the rotation. Withdrawals that require an unavailable key return a service error until the key is restored.
3. With contribution writers stopped, run `python -m trailforge.worker.community_keys rekey`, then restart writers with the new key. The command migrates linked observations and off-network candidates together. It also recovers pending legacy links when you supply a retained key after the migration. Run the check again with the retired keys removed before resuming traffic.

When no retained key matches a legacy contribution, those rows remain outside public grades and off-network overlays. The owner can confirm sharing again using the saved trimmed trip. Recovering the old rows for owner withdrawal still requires the original key and trip nonce. Run recovery before changing the visibility or deleting an unlinked legacy trip if you still need to recover its old rows.

## Hazard votes

Resolving a condition report requires at least three votes from accounts that were seven days old when they voted, and those votes must outnumber confirmations. Votes from newer accounts still appear in the report's vote tally.
