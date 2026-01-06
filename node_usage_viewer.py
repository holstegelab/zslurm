import argparse
import csv
import datetime as dt
import glob
import json
import os
import sys
import traceback
from collections import defaultdict


def parse_args():
    p = argparse.ArgumentParser(
        description="Build an interactive HTML viewer for node usage fill rates from node_usage-*.tsv logs."
    )
    p.add_argument(
        "-i",
        "--input",
        action="append",
        default=["node_usage-*.tsv"],
        help="Glob pattern for node usage TSV files (can be repeated). Default: node_usage-*.tsv",
    )
    p.add_argument(
        "-o",
        "--output",
        default="node_usage_report.html",
        help="Path to write the HTML report. Default: node_usage_report.html",
    )
    p.add_argument(
        "--bin-sec",
        type=int,
        default=60,
        help="Bin size in seconds for time aggregation. Default: 60",
    )
    p.add_argument(
        "--include-partition",
        action="append",
        default=None,
        help="Only include these partitions (repeatable). If omitted, include all.",
    )
    p.add_argument(
        "--include-unmanaged",
        action="store_true",
        help="Also include unmanaged engines (managed==0). Default: exclude",
    )
    p.add_argument(
        "--include-stopping",
        action="store_true",
        help="Also include engines marked stopping==1. Default: exclude",
    )
    p.add_argument(
        "--include-phasing-out",
        action="store_true",
        help="Also include engines with status PHASING_OUT. Default: exclude",
    )
    return p.parse_args()


def read_rows(files):
    for fp in files:
        with open(fp, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            header = reader.fieldnames or []
            required = {
                "ts_iso",
                "engine_id",
                "partition",
                "status",
                "managed",
                "stopping",
                "cores",
                "totmem_mb",
                "res_cores_reserved",
                "res_mem_reserved_mb",
            }
            missing = [k for k in required if k not in header]
            if missing:
                raise RuntimeError(
                    f"Required columns missing in {fp}: {missing}. Did you run a recent zslurm with node reports enabled?"
                )
            for row in reader:
                yield row


def floor_bin(ts: dt.datetime, bin_sec: int) -> int:
    return int(ts.timestamp()) // bin_sec * bin_sec


def to_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return float(default)


def to_int(x, default=0):
    try:
        return int(x)
    except Exception:
        try:
            return int(float(x))
        except Exception:
            return int(default)


def aggregate(rows, bin_sec, include_parts, include_unmanaged, include_stopping, include_phasing_out):
    bins = defaultdict(lambda: defaultdict(lambda: {
        "tot_cores": 0.0,
        "res_cores": 0.0,
        "tot_mem_mb": 0.0,
        "res_mem_mb": 0.0,
    }))
    partitions_seen = set()

    for r in rows:
        try:
            ts = dt.datetime.fromisoformat(r["ts_iso"])  # no timezone in logs
        except Exception:
            # try to parse without microseconds
            try:
                ts = dt.datetime.strptime(r["ts_iso"], "%Y-%m-%dT%H:%M:%S")
            except Exception:
                raise
        part = r.get("partition", "")
        if include_parts is not None and part not in include_parts:
            continue
        managed = to_int(r.get("managed", 0))
        stopping = to_int(r.get("stopping", 0))
        status = (r.get("status", "") or "").strip()
        if not include_unmanaged and managed == 0:
            continue
        if not include_stopping and stopping == 1:
            continue
        if not include_phasing_out and status == "PHASING_OUT":
            continue

        b = floor_bin(ts, bin_sec)
        tot_cores = to_float(r.get("cores", 0.0))
        res_cores = to_float(r.get("res_cores_reserved", 0.0))
        tot_mem_mb = to_float(r.get("totmem_mb", 0.0))
        res_mem_mb = to_float(r.get("res_mem_reserved_mb", 0.0))

        # Per-partition
        agg = bins[b][part]
        agg["tot_cores"] += tot_cores
        agg["res_cores"] += max(0.0, res_cores)
        agg["tot_mem_mb"] += tot_mem_mb
        agg["res_mem_mb"] += max(0.0, res_mem_mb)

        # Global (ALL)
        agg_all = bins[b]["ALL"]
        agg_all["tot_cores"] += tot_cores
        agg_all["res_cores"] += max(0.0, res_cores)
        agg_all["tot_mem_mb"] += tot_mem_mb
        agg_all["res_mem_mb"] += max(0.0, res_mem_mb)

        partitions_seen.add(part)

    return bins, sorted(list(partitions_seen))


def build_series(bins, partitions, bin_sec):
    timestamps = sorted(bins.keys())
    def pct(n, d):
        return (100.0 * n / d) if d > 0 else 0.0

    series = {"cores": {}, "mem": {}}
    labels = [dt.datetime.fromtimestamp(t).isoformat() for t in timestamps]

    for part in ["ALL"] + partitions:
        cores_vals = []
        mem_vals = []
        for t in timestamps:
            agg = bins[t].get(part, {"tot_cores": 0.0, "res_cores": 0.0, "tot_mem_mb": 0.0, "res_mem_mb": 0.0})
            cores_vals.append(pct(agg["res_cores"], agg["tot_cores"]))
            mem_vals.append(pct(agg["res_mem_mb"], agg["tot_mem_mb"]))
        series["cores"][part] = cores_vals
        series["mem"][part] = mem_vals

    return labels, series


def render_html(output_path, labels, series, partitions):
    payload = {
        "labels": labels,
        "series": series,
        "partitions": ["ALL"] + partitions,
    }
    html = f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Node Usage Fill Rates</title>
  <style>
    body {{ font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; margin: 24px; }}
    h1 {{ margin-bottom: 4px; }}
    .desc {{ color: #555; margin-bottom: 16px; }}
    .row {{ display: flex; gap: 24px; flex-wrap: wrap; }}
    .card {{ flex: 1 1 600px; min-width: 320px; padding: 16px; border: 1px solid #e5e7eb; border-radius: 8px; }}
    canvas {{ max-width: 100%; height: 360px; }}
    .legend {{ margin-top: 8px; font-size: 14px; }}
  </style>
  <script src=\"https://cdn.jsdelivr.net/npm/chart.js\"></script>
  <script src=\"https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns\"></script>
</head>
<body>
  <h1>Node Usage Fill Rates</h1>
  <div class=\"desc\">Reserved vs total capacity, aggregated by time bin and partition.</div>
  <div class=\"row\">
    <div class=\"card\">
      <h3>Core fill (%)</h3>
      <canvas id=\"coresChart\"></canvas>
    </div>
    <div class=\"card\">
      <h3>Memory fill (%)</h3>
      <canvas id=\"memChart\"></canvas>
    </div>
  </div>
  <script id=\"data\" type=\"application/json\">{json.dumps(payload)}</script>
  <script>
    const payload = JSON.parse(document.getElementById('data').textContent);
    const labels = payload.labels.map(ts => new Date(ts));
    function color(i) {{
      const colors = [
        '#2563eb','#10b981','#f59e0b','#ef4444','#8b5cf6','#06b6d4','#84cc16','#d946ef',
        '#dc2626','#7c3aed','#0ea5e9','#f97316','#16a34a','#e11d48'
      ];
      return colors[i % colors.length];
    }}
    function makeDatasets(kind) {{
      const parts = payload.partitions;
      const ds = [];
      for (let i = 0; i < parts.length; i++) {{
        const p = parts[i];
        const y = payload.series[kind][p] || [];
        ds.push({{
          label: (p === 'ALL' ? 'ALL' : p),
          data: y.map((v, idx) => ({{ x: labels[idx], y: v }})),
          borderColor: color(i),
          backgroundColor: color(i) + '33',
          borderWidth: 2,
          pointRadius: 0,
          tension: 0.15,
        }});
      }}
      return ds;
    }}
    function makeChart(canvasId, kind) {{
      const ctx = document.getElementById(canvasId).getContext('2d');
      return new Chart(ctx, {{
        type: 'line',
        data: {{ datasets: makeDatasets(kind) }},
        options: {{
          responsive: true,
          interaction: {{ mode: 'nearest', intersect: false }},
          scales: {{
            x: {{ type: 'time', time: {{ unit: 'minute' }} }},
            y: {{ beginAtZero: true, max: 100, title: {{ display: true, text: kind === 'cores' ? 'Core fill (%)' : 'Memory fill (%)' }} }},
          }},
          plugins: {{
            legend: {{ position: 'bottom' }},
            tooltip: {{ callbacks: {{
              label: function(context) {{
                const v = (typeof context.parsed.y === 'number') ? context.parsed.y.toFixed(2) : context.parsed.y;
                return context.dataset.label + ': ' + v + '%';
              }}
            }}}}
          }}
        }});
      }};
    }}
    makeChart('coresChart', 'cores');
    makeChart('memChart', 'mem');
  </script>
</body>
</html>
"""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    args = parse_args()
    try:
        patterns = args.input if isinstance(args.input, list) else [args.input]
        files = []
        for pat in patterns:
            files.extend(sorted(glob.glob(pat)))
        files = sorted(set(files))
        if not files:
            raise FileNotFoundError("No input files matched. Provide -i node_usage-*.tsv (ensure zslurm wrote node reports).")

        rows = list(read_rows(files))
        bins, parts = aggregate(
            rows,
            bin_sec=args.bin_sec,
            include_parts=set(args.include_partition) if args.include_partition else None,
            include_unmanaged=args.include_unmanaged,
            include_stopping=args.include_stopping,
            include_phasing_out=args.include_phasing_out,
        )
        labels, series = build_series(bins, parts, args.bin_sec)
        render_html(args.output, labels, series, parts)
        print(f"Wrote {args.output}")
    except Exception as ex:
        print(str(ex), file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
