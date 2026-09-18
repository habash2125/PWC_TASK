import Plotly from "plotly.js-dist-min";
import type { Data, Layout } from "plotly.js";
import createPlotlyComponent from "react-plotly.js/factory";
import type { ChartSpec } from "@/types/api";

const Plot = createPlotlyComponent(Plotly);

const BASE_LAYOUT = {
  margin: { l: 56, r: 24, t: 48, b: 56 },
  paper_bgcolor: "rgba(0,0,0,0)",
  plot_bgcolor: "rgba(0,0,0,0)",
  font: { family: "Inter, system-ui, sans-serif", size: 12, color: "#334155" },
  colorway: ["#2563eb", "#f59e0b", "#10b981", "#ef4444", "#8b5cf6", "#0ea5e9", "#f97316", "#14b8a6"],
  autosize: true,
};

export function PlotlyChart({ spec, height, title }: { spec: ChartSpec; height?: number; title?: string }) {
  const layout = { ...BASE_LAYOUT, ...spec.layout, ...(title ? { title: { text: title } } : {}), autosize: true };
  return (
    <Plot
      data={spec.data as Data[]}
      layout={layout as Partial<Layout>}
      useResizeHandler
      style={{ width: "100%", height: height ?? 360 }}
      config={{ displaylogo: false, responsive: true, modeBarButtonsToRemove: ["lasso2d", "select2d"] }}
    />
  );
}
