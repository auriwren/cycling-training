# UX Review: Cycling Training Dashboard

**Reviewer:** Senior UI/UX Designer (automated review)
**Date:** February 12, 2026
**URL:** https://www.eiwe.me/cycling-dashboard.html
**Screenshot:** Captured via Playwright at 1440x900 viewport, full-page

---

## Executive Summary

This is an impressively comprehensive cycling analytics dashboard that consolidates training load, recovery data, race planning, and coaching analysis into a single scrollable page. The dark theme is well-executed and appropriate for a data-heavy sports analytics context. However, the dashboard suffers from **information overload**, **flat visual hierarchy**, and **long scroll depth** that buries high-value content. The core data is excellent; the presentation needs restructuring to match how a cyclist actually uses this information day-to-day.

**Overall Grade: B-** — Strong data foundation, needs UX restructuring.

---

## 1. Visual Hierarchy

### What Works
- The header KPI strip (countdown timers, FTP, CTL/ATL/TSB) is well-positioned and immediately scannable. A cyclist opening this page gets the "vital signs" at a glance.
- The countdown boxes for Halvvattern (114 days) and Vatternrundan (121 days) use larger type and gradient backgrounds, making them visually dominant — appropriate since race countdown is the key motivator.
- Color-coded badges (green/yellow/red) on the weekly workout table draw the eye to workout quality.

### Problems
- **Hierarchy is essentially flat.** Every card, every section title, every chart competes for equal attention. There's no visual distinction between "check this daily" content (this week's workouts, recovery) and "reference occasionally" content (race plan details, 2025 comparisons).
- **Section headings are uniform.** "Weekly Training Load," "Recovery Dashboard," "Coaching Assessment," and "Vatternrundan Race Plan" all use the same `h2` styling. Primary sections and sub-sections read at the same level.
- **The coaching text** — arguably the highest-value synthesized content — is buried in dense paragraphs deep in the scroll. A cyclist scanning quickly will skip it entirely.
- **No "what needs my attention today" signal.** The dashboard shows everything equally; it should surface what's changed, what's concerning, or what's next.

### Recommendation
Add a prominent "Today's Focus" or alert section near the top that surfaces 2-3 actionable items (e.g., "Next workout: Friday Endurance, TSS 54" / "Recovery trending down 10% — monitor sleep" / "FTP test in 14 days"). Push reference content (race plan, historical comparisons) into collapsible sections or separate tabs.

---

## 2. Information Density

### Assessment: Too Dense

The page scrolls for approximately 6-7 full viewport heights. That's a lot of content for a dashboard meant to be checked regularly. Key density issues:

- **The Race Plan section** contains: segment power targets, 2025 comparison table, depot stop timeline, fueling timeline table, taper timeline table, hourly nutrition plan, and multiple insight cards. This is an entire race plan document embedded in a daily dashboard.
- **The Coaching Assessment** is ~800 words of prose. Valuable content, but it reads like a written report, not a dashboard element.
- **Mini-stat grids** are used extensively (sometimes 3-4 rows of 4 stats each in a single card). While each individual stat is legible, the cumulative effect is overwhelming.
- **Halvvattern section** duplicates structural patterns from the Vatternrundan section (stop timelines, stat grids, insight cards), adding scroll depth without proportional value.

### Recommendation
1. **Collapse by default:** Race plan, coaching assessment, and Halvvattern details should be expandable/collapsible sections. Show a one-line summary with a "Show details" toggle.
2. **Progressive disclosure:** The daily view should show: header KPIs, this week's schedule, recovery snapshot, and key alerts. Everything else should be one click away.
3. **Consider tabs or navigation:** Split into "Daily" / "Training Load" / "Race Plan" / "Recovery" views.

---

## 3. Color Usage

### What Works Well
- **Dark theme execution is strong.** The `#0f1117` background with `#1a1d27` cards creates good depth and contrast without being harsh. This is a well-chosen dark palette.
- **Recovery bracket colors** (red/yellow/green) are intuitive and match the Whoop color system the user is already familiar with.
- **The blue-cyan gradient** on the header title and countdown boxes creates a cohesive brand feel.
- **Chart colors** are distinct: blue for CTL, red for ATL, green for TSB — standard PMC conventions that any cyclist would recognize.
- **Status badges** use appropriate semantic colors: green for good quality (89), yellow for moderate (74).

### Problems
- **Too many accent colors in play.** The palette uses blue, cyan, red, green, yellow, orange, and purple all as accent colors. In the mini-stat grids especially, the rainbow effect reduces the signal value of any individual color.
- **The "finding box" green border** (recovery correlation insight) uses the same green as positive status indicators, but it's informational, not positive/negative. This conflates information type with status.
- **Purple for workout quality** is an unusual choice. Quality is a performance metric; blue (matching the training load theme) would be more intuitive.

### Recommendation
Reduce the accent palette to 3-4 colors max: blue (primary/training), green (positive/on-track), red/orange (warning/attention), and gray for neutral. Reserve purple and cyan for specific, consistent purposes only.

---

## 4. Typography and Readability

### What Works
- **System font stack** (`-apple-system, BlinkMacSystemFont, Segoe UI, Roboto`) is appropriate for a data dashboard. Clean, no-nonsense.
- **Line-height of 1.6** on body text is comfortable for the dark background.
- **11px uppercase labels** on stat boxes are a good pattern for dashboard KPIs — clearly secondary to the values.
- **Font weight differentiation** is used well: 700 for values, 500-600 for headings, 400 for body.

### Problems
- **13px body text** in the coaching assessment and insight cards is small for extended reading on a dark background. Dark themes need slightly larger text to maintain readability because light-on-dark has inherently lower perceived contrast.
- **The coaching text section** uses `14px` with `line-height: 1.75`, which is better, but the paragraphs are long and dense. No visual breaks, pull-quotes, or emphasis beyond bold keywords.
- **Table text at 13px** is tight. The segment tables and workout tables would benefit from 14px with slightly more cell padding.
- **11px labels** are at the minimum legible size. On high-DPI displays they're fine; on standard displays they may strain.

### Recommendation
Bump body text to 14-15px minimum. Break the coaching assessment into scannable sections with subheadings. Add more vertical whitespace between paragraphs in dense text sections.

---

## 5. Data Visualization

### What Works
- **Chart.js charts are clean and functional.** The bar chart for weekly TSS effectively shows training volume over 12 months, with the flu week highlighted in red — a nice touch.
- **PMC chart** uses the standard CTL/ATL/TSB convention with appropriate colors. The filled area under CTL creates good visual weight.
- **Recovery bar chart** with green/yellow/red coloring per bar is immediately readable — you can see the distribution at a glance.
- **FTP projection line chart** with the 300W target as a dashed line is a clear goal visualization.

### Problems
- **Charts are small.** The recovery, HRV, and sleep charts at 200px height are cramped. With 31 data points, the bars and points are tiny. The x-axis labels overlap at this size.
- **The quality-over-time chart** (180px) is too small to show meaningful trends in what is actually interesting data. The y-axis range of 60-100 compresses the visual variance.
- **No interactive tooltips visible** in the screenshot (though Chart.js supports them). For a dashboard this data-dense, hover details are essential.
- **The FTP timeline** (dot-and-line progression from 263W → 300W) is a nice concept but the horizontal line/dots render inconsistently. The gradient bar behind the dots is thin and gets lost.
- **Missing chart: training volume by zone or type.** For a cyclist, knowing the distribution of endurance vs. sweetspot vs. threshold work is as important as total TSS.

### Recommendation
1. Increase chart heights to 250-300px minimum for the recovery row.
2. Add a power zone distribution chart or weekly workout type breakdown.
3. Consider sparklines for the mini-stat sections (e.g., a tiny trend line next to "7-Day Avg Recovery: 60%") instead of static numbers.
4. The weekly TSS bar chart with 51 weeks of data is too compressed. Consider showing only the last 16-20 weeks with an option to expand.

---

## 6. Mobile Responsiveness

### Assessment: Partially Addressed

The CSS includes a media query at `max-width: 900px` that collapses the 2-column and 3-column grids to single column. This is the bare minimum for responsiveness.

### Problems
- **The header stats row** will wrap awkwardly on narrow screens. Six stat boxes plus two countdown boxes is a lot of horizontal content to reflow.
- **Tables** (segment tables, workout tables, fueling timeline) have no responsive strategy. They'll overflow on mobile or compress to unreadable widths.
- **Chart.js charts** are responsive by default, but at mobile widths the x-axis labels for 30-51 data points will be unreadable.
- **The stop-timeline** with its left-border visual treatment should work on mobile, but the text may get tight.
- **No touch considerations.** No hamburger menu, no swipe gestures, no sticky header for context while scrolling.

### Recommendation
1. Add a sticky header with key KPIs (FTP, CTL, countdown) that remains visible while scrolling on mobile.
2. Use horizontal scroll containers for tables on mobile rather than trying to compress them.
3. Reduce chart data points on mobile (e.g., show 12 weeks of TSS instead of 51).
4. Consider a mobile-first redesign with tab navigation: the current page is fundamentally a desktop experience.

---

## 7. Navigation / Flow

### Current Section Order
1. Header KPIs + countdown
2. Weekly Training Load (12 months) + PMC chart
3. Recovery Dashboard (30 days)
4. Workout Quality + FTP Trajectory
5. Vatternrundan Race Plan (detailed)
6. Halvvattern Race Plan (detailed)
7. Coaching Assessment (long prose)
8. Key Insights + This Week

### Problems
- **"This Week" is at the bottom.** This is the most time-sensitive, frequently-checked content. It should be near the top, right after the header KPIs. A cyclist opens this dashboard asking "what's my workout today?" and has to scroll past race plans they've already memorized.
- **Coaching Assessment is buried.** If the coach's analysis changes weekly, it should be more prominent. If it's static/monthly, it could be a separate page.
- **Key Insights at the bottom** are redundant with content scattered throughout (the recovery correlation finding appears in both the Recovery Dashboard section and the Insights section).
- **No table of contents or section navigation.** With 7+ major sections, anchor links or a sidebar nav would help.

### Recommended Order (Daily Use)
1. Header KPIs (keep as-is)
2. **This Week** (workouts, weekly TSS progress, recovery snapshot)
3. Recovery Dashboard (30 days)
4. Training Load + PMC (trends)
5. FTP Trajectory
6. Key Insights
7. Race Plans (collapsible, reference content)
8. Coaching Assessment (collapsible or separate page)

---

## 8. Consistency

### What Works
- **Card styling is consistent:** Same border-radius (12px), padding (24px), background, and border throughout.
- **Mini-stat grids** use the same pattern everywhere: value on top, uppercase label below.
- **Badge styling** is consistent for status indicators.
- **Insight cards** maintain the same left-border + tinted background pattern throughout.

### Problems
- **Inconsistent section nesting.** Some content uses the grid system (2-column), some uses full-width cards, and the Recovery Dashboard nests a 3-column grid inside a full-width card. The nesting levels aren't predictable.
- **Mixed content types in single cards.** The Workout Quality card contains: recovery brackets, a finding box, a chart, and a mini-stats grid. The FTP card contains: a custom timeline widget, mini-stats, and a chart. These are structurally different but placed side-by-side.
- **Stop timeline styling** appears in both Vatternrundan and Halvvattern sections but with slightly different detail levels, which is fine but means the Halvvattern section feels like a partial copy-paste.
- **The "finding box"** appears with different color schemes (green border, purple border) without a clear system for when each color is used.
- **Some inline styles** override the CSS classes (e.g., `style="height:200px"` on chart containers, `style="margin-bottom:0"` on grids), suggesting the page was built iteratively rather than with a systematic component library.

### Recommendation
Establish a clear component system: "stat card," "chart card," "timeline card," "insight card," "data table card." Each component type should have one visual treatment. Build sections by composing these components rather than creating unique layouts per section.

---

## 9. Specific Improvement Recommendations

### High Priority (Impact + Feasibility)

1. **Move "This Week" to position #2**, right below the header. This is the #1 daily use case. Add a "Today's Workout" callout with the next scheduled session prominently displayed.

2. **Make race plan sections collapsible.** Add a summary line (e.g., "Vatternrundan: 315km, June 13, Sub-10h target") with a disclosure triangle to expand the full plan. Default to collapsed. This alone would cut visible page length by ~40%.

3. **Add section navigation.** A simple sticky left sidebar (desktop) or top tab bar (mobile) with section anchors: Overview | This Week | Recovery | Training | Race Plans | Coach Notes.

4. **Reduce the coaching prose.** Extract the 3 most actionable bullet points and display them prominently. Link to the full assessment as an expandable section or separate page.

5. **Increase chart sizes in the recovery row.** The 200px height with 31 days of data is too compressed. Bump to 260-280px.

### Medium Priority

6. **Add a "days since last workout" or "next workout" indicator** to the header KPI strip. This is more actionable than showing both ATL and TSB simultaneously.

7. **Consolidate duplicate insights.** The recovery-quality correlation appears in 3 places (Recovery Dashboard, Workout Quality card, Key Insights). Show it once, prominently.

8. **Add zone distribution visualization.** A pie chart or stacked bar showing time-in-zone (endurance, tempo, sweetspot, threshold, VO2max) would be more useful than some of the current mini-stat grids.

9. **Reduce color palette** to 4 functional colors. Current 7-color palette dilutes the meaning of each color.

10. **Optimize the weekly TSS chart** to show 20 weeks by default with a "Show full year" toggle. 51 narrow bars are hard to read.

### Lower Priority (Polish)

11. **Add subtle animations** on page load (fade-in cards, count-up on KPI numbers) to create a more polished feel.

12. **Add print/export styles.** A cyclist might want to print the race plan section for reference on race day.

13. **Dark/light mode toggle.** The dark theme is good but having a light option improves accessibility and outdoor readability.

14. **Add data freshness indicator.** Show when data was last synced from TrainingPeaks/Whoop (e.g., "Last updated: 2 hours ago").

---

## Summary

This dashboard is built by someone who deeply understands cycling training data. The analytical depth is exceptional — the recovery-quality correlation analysis, the PMC modeling, the detailed race plans with fueling timelines — this is genuinely useful content.

The UX challenge is **curation, not creation.** The data is all here; it just needs to be organized by frequency of use (daily → weekly → monthly → reference) and presented with progressive disclosure so the cyclist can quickly answer "how am I doing today?" without scrolling through race plans they've already internalized.

The dark theme, card-based layout, and Chart.js visualizations provide a solid foundation. With the restructuring described above — particularly moving "This Week" up, collapsing reference sections, and adding navigation — this could go from a comprehensive data dump to a genuinely excellent training dashboard.
