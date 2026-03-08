Execute `python3 scripts/estimate_effort_v2.py --format json --cache` and parse the JSON output. Then perform the following updates:

**1. README.md Updates:**
- Locate the "Project Status" or "Development Progress" section
- Replace/update the development time line with:
  `Development time: ~{recommended_hours} hours ({total_commits} commits across {active_days} active days)`
- Add/update velocity metrics line:
  `Current pace: {avg_commits_per_day:.1f} commits/day, {story_completion_rate:.0%} issues resolved`
- Add/update efficiency line:
  `Development intensity: {development_intensity:.1f} activities/day over {project_span_days} day span`

**2. STORIES.md Updates:**
Add new entry at the top in this exact format:
```
## {YYYY-MM-DD} - Development Time Analysis
**Total Hours:** {recommended_hours:.1f}h (threshold: {threshold_minutes}min)
**Velocity:** {avg_commits_per_day:.1f} commits/day | {avg_activities_per_day:.1f} activities/day
**Progress:** {closed_issues}/{total_issues} issues completed ({story_completion_rate:.0%})
**Span:** {active_days} active days over {project_span_days} calendar days
**Trend:** [Compare metrics to previous entry - flag significant changes]

```

**3. Validation & Error Handling:**
- If script execution fails, check:
  1. Git repository status (`git status`)
  2. GitHub CLI authentication (`gh auth status`)
  3. Retry with `--no-github --cache` flags
- Flag anomalies:
  - Hours jumped >25% from previous update
  - Velocity dropped >30% from previous entry
  - Zero commits in analysis period
  - Unrealistic activity patterns (>12 hours/day average)
- If no previous STORIES.md entries exist, note this is the baseline measurement

**4. Post-Update Actions:**
- Verify both files were updated successfully
- Check that all placeholders were replaced with actual values
- Ensure formatting is consistent with existing documentation style
- Confirm dates and calculations are logical

**Expected JSON Structure Reference:**
```json
{
  "metadata": {"analysis_date": "...", "threshold_minutes": 120},
  "metrics": {
    "total_commits": N,
    "active_days": N,
    "avg_commits_per_day": N.N,
    "story_completion_rate": N.NN,
    "development_intensity": N.N,
    "project_span_days": N
  },
  "summary": {"recommended_hours": N.N}
}
```

If JSON parsing fails, fall back to text output and extract metrics manually using regex patterns.
