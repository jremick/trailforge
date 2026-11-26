"""Curated MCP Prompts for TrailForge 4WD trip planning, safety checks, and rig matching."""

from __future__ import annotations

from mcp import types


def list_mcp_prompts() -> list[types.Prompt]:
    """Return the 3 canonical TrailForge MCP prompts (Section 6.1 F)."""
    return [
        types.Prompt(
            name="plan-weekend-trip",
            title="Plan a 4WD Weekend Trip",
            description=(
                "Check closures and rainfall first, discover open candidate tracks with "
                "camps/huts, chain them with plan_route, and deliver map, elevation profile, "
                "safety briefing, and GPX/GridForge handoff."
            ),
            arguments=[
                types.PromptArgument(
                    name="region_or_area",
                    description=(
                        "Target region, park, or town (e.g. 'Australian Alps', 'Blue Mountains')."
                    ),
                    required=True,
                ),
                types.PromptArgument(
                    name="days",
                    description="Trip duration in days (default: 2).",
                    required=False,
                ),
                types.PromptArgument(
                    name="vehicle_class",
                    description=(
                        "Optional rig capability override "
                        "(AWD, Stock 4WD, Touring 4WD, Modified 4WD)."
                    ),
                    required=False,
                ),
                types.PromptArgument(
                    name="difficulty",
                    description="Desired difficulty tier(s) (e.g. 'Easy,Medium' or 'Hard').",
                    required=False,
                ),
                types.PromptArgument(
                    name="preferences",
                    description=(
                        "Optional preferences (e.g. 'river camps, alpine huts, "
                        "avoid extreme slopes')."
                    ),
                    required=False,
                ),
            ],
        ),
        types.Prompt(
            name="pre-trip-safety-check",
            title="Pre-Trip Safety & Closure Check",
            description=(
                "Audit active agency closures, OSM/community locked gates, 72 h rainfall "
                "wet-risk, unbridged perennial waterway crossings, and rig suitability."
            ),
            arguments=[
                types.PromptArgument(
                    name="track_or_route",
                    description="Track name, canonical track_id, or comma-separated track IDs.",
                    required=True,
                ),
                types.PromptArgument(
                    name="vehicle_class",
                    description=(
                        "Optional vehicle capability class "
                        "(AWD, Stock 4WD, Touring 4WD, Modified 4WD)."
                    ),
                    required=False,
                ),
            ],
        ),
        types.Prompt(
            name="find-tracks-for-rig",
            title="Find & Compare Tracks Suited to Your Rig",
            description=(
                "Resolve the user's vehicle profile (or requested vehicle_class), find open "
                "suitable tracks, and compare top candidates side-by-side."
            ),
            arguments=[
                types.PromptArgument(
                    name="region_or_area",
                    description="Region, state, or park to search.",
                    required=True,
                ),
                types.PromptArgument(
                    name="vehicle_class",
                    description=(
                        "Optional rig class override "
                        "(AWD, Stock 4WD, Touring 4WD, Modified 4WD)."
                    ),
                    required=False,
                ),
                types.PromptArgument(
                    name="max_difficulty",
                    description="Optional maximum difficulty tier (Easy, Medium, Hard, Extreme).",
                    required=False,
                ),
            ],
        ),
    ]


def get_mcp_prompt(
    name: str,
    arguments: dict[str, str] | None = None,
) -> types.GetPromptResult:
    """Render prompt messages for one of TrailForge's 3 MCP prompts."""
    args = arguments or {}

    if name == "plan-weekend-trip":
        region = (args.get("region_or_area") or "").strip()
        if not region:
            raise ValueError("plan-weekend-trip requires 'region_or_area'")
        days = (args.get("days") or "2").strip()
        vehicle_class = (args.get("vehicle_class") or "").strip()
        difficulty = (args.get("difficulty") or "").strip()
        preferences = (args.get("preferences") or "").strip()

        rig_note = (
            f"Use `vehicle_class='{vehicle_class}'` for suitability evaluation."
            if vehicle_class
            else (
                "If the user is signed in, check `get_my_garage` first to resolve their "
                "active rig; otherwise state suitability assumptions clearly."
            )
        )
        diff_note = f" Target difficulty: {difficulty}." if difficulty else ""
        pref_note = f" Preferences: {preferences}." if preferences else ""

        prompt_text = (
            f"Plan a {days}-day 4WD trip in **{region}**.{diff_note}{pref_note}\n\n"
            "Follow this exact TrailForge workflow:\n"
            f"1. Call `check_area_conditions` for `{region}` first to surface active agency "
            "closures, bushfire/flood alerts, community locked-gate warnings, and 72 h "
            "rainfall wet-risk.\n"
            f"2. {rig_note}\n"
            "3. Call `search_trails` with `open_only=true` (and `poi_types=['campground','hut']` "
            "when overnight camping is relevant) to find realistic candidate tracks.\n"
            "4. Chain the best ordered tracks with `plan_route` (with `include_images=true`) "
            "to inspect stitched distance, elevation profile, leg gaps, crossings, and "
            "rig suitability.\n"
            "5. If helpful for offline GPS/print prep, offer `export_route_file` (GPX 1.1) and "
            "include the TrailForge web deep link plus GridForge 300 DPI strip-atlas command.\n"
            "6. Present a concise briefing covering: verdict, day-by-day route legs, "
            "campsites/huts, explicit safety/wet-risk/crossing warnings, deep link, and "
            "mandatory data attributions."
        )
        return types.GetPromptResult(
            description=f"Guided {days}-day 4WD trip planning workflow for {region}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt_text),
                )
            ],
        )

    if name == "pre-trip-safety-check":
        target = (args.get("track_or_route") or "").strip()
        if not target:
            raise ValueError("pre-trip-safety-check requires 'track_or_route'")
        vehicle_class = (args.get("vehicle_class") or "").strip()
        rig_clause = (
            f" Evaluate specifically for `{vehicle_class}`."
            if vehicle_class
            else " Check `get_my_garage` if authenticated to apply the user's active rig profile."
        )
        prompt_text = (
            f"Run a thorough TrailForge pre-trip safety check for **{target}**.{rig_clause}\n\n"
            "Steps:\n"
            "1. If multiple track IDs are given, call `plan_route`; otherwise call `inspect_track` "
            "(resolving by `track_id` or `track_name`) with `include_images=true`.\n"
            "2. Also call `check_area_conditions` for the track's bounding box/state to catch "
            "nearby park alerts or regional fire/flood incidents.\n"
            "3. Report clearly on:\n"
            "   - **Blocking official closures & seasonal dates** (with official agency URLs)\n"
            "   - **OSM & community locked-gate / private-property warnings**\n"
            "   - **72 h rainfall wet-risk** and clay/mud traction shift\n"
            "   - **Unbridged perennial water crossings** (depth & snorkel advice)\n"
            "   - **Steep climbs/descents** (`max_slope_deg`, snowline elevation)\n"
            "   - **Rig suitability rating & recovery gear recommendations**\n"
            "4. Include the TrailForge deep link and preserve all required "
            "OSM/GA/state/Open-Meteo attributions."
        )
        return types.GetPromptResult(
            description=f"Pre-trip safety audit for {target}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt_text),
                )
            ],
        )

    if name == "find-tracks-for-rig":
        region = (args.get("region_or_area") or "").strip()
        if not region:
            raise ValueError("find-tracks-for-rig requires 'region_or_area'")
        vehicle_class = (args.get("vehicle_class") or "").strip()
        max_diff = (args.get("max_difficulty") or "").strip()

        rig_step = (
            f"1. Use `vehicle_class='{vehicle_class}'`."
            if vehicle_class
            else (
                "1. Call `get_my_garage` first if signed in to identify the user's default "
                "vehicle class and snorkel/locker setup; if anonymous, ask or assume "
                "`Stock 4WD` and note the assumption."
            )
        )
        diff_clause = f" Cap grades at `{max_diff}`." if max_diff else ""

        prompt_text = (
            f"Find and compare open 4WD tracks in **{region}** suited to the user's rig."
            f"{diff_clause}\n\n"
            "Steps:\n"
            f"{rig_step}\n"
            f"2. Call `search_trails` for `{region}` with `open_only=true` and the resolved "
            "`vehicle_class`.\n"
            "3. Pick the top 2–4 suitable open candidates and call `compare_tracks` "
            "(`include_images=true`) for an overlaid elevation & KPI comparison.\n"
            "4. Summarize each candidate's grade, suitability verdict, length, climb, max slope, "
            "waterway crossings, wet-risk status, TrailForge deep link, and required attributions."
        )
        return types.GetPromptResult(
            description=f"Rig-matched track discovery and comparison in {region}",
            messages=[
                types.PromptMessage(
                    role="user",
                    content=types.TextContent(type="text", text=prompt_text),
                )
            ],
        )

    raise ValueError(f"Unknown TrailForge MCP prompt: {name!r}")
