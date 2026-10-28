/*
 * The ECharts subset the evals chart needs, registered once. Only ever
 * reached through `import("./echarts")`, so it is a separate chunk loaded
 * on the client after the chart mounts, never part of first-load JS.
 */
import { LineChart, ScatterChart } from "echarts/charts";
import {
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { init, use } from "echarts/core";
import { SVGRenderer } from "echarts/renderers";

use([
  ScatterChart,
  LineChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  SVGRenderer,
]);

export { init };
