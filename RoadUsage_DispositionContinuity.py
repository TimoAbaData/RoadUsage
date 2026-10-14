"""Keep short disposition crossings from fragmenting road attribution."""

from collections import defaultdict


def merge_spans(spans, tolerance=1e-7):
    """Union travelled intervals in metres while preserving their order."""
    merged = []
    for start, end in sorted(spans):
        if end - start <= tolerance:
            continue
        if merged and start <= merged[-1][1] + tolerance:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def smooth_short_intervals(intervals_by_label, maximum_m, adjacency_m=0.25, protected_by_label=None):
    """Reassign short continuous runs to the longest touching longer run.

    Intervals are distances along one oriented line. A short run with no
    adjacent longer run is omitted. A run of exactly maximum_m is short.
    Long overlapping runs remain independent for overlap QC. A run
    protected by the Step 1 road attribute stays even on a partial route.
    """
    if maximum_m < 0 or adjacency_m < 0:
        raise ValueError("Disposition overlap distances must be nonnegative")
    runs = [(label, start, end)
            for label, spans in intervals_by_label.items()
            for start, end in merge_spans(spans)]
    protected_by_label = protected_by_label or {}
    def protected(run):
        return any(min(run[2], end) - max(run[1], start) > 1e-7
                   for start, end in protected_by_label.get(run[0], ()))
    major = [run for run in runs if run[2] - run[1] > maximum_m + 1e-7 or protected(run)]
    output = defaultdict(list)
    for label, start, end in runs:
        if end - start > maximum_m + 1e-7 or protected((label, start, end)):
            output[label].append((start, end))
            continue
        candidates = []
        for other_label, other_start, other_end in major:
            if other_label == label:
                continue
            gap = max(other_start - end, start - other_end, 0.0)
            if gap <= adjacency_m + 1e-7:
                candidates.append((-(other_end - other_start), gap,
                                   other_start, str(other_label), other_label))
        if candidates:
            winner = min(candidates)[-1]
            output[winner].append((start, end))
    return {label: merge_spans(spans) for label, spans in output.items()}
