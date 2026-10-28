import type { Metadata } from "next";
import { RunSidePanel } from "@/components/panel/SidePanel";
import { AppShell } from "@/components/shell/AppShell";
import { NewRunComposer } from "@/components/thread/NewRunComposer";
import { RunThread } from "@/components/thread/RunThread";
import { RunStreamProvider } from "@/components/thread/run-stream";

export async function generateMetadata({
  params,
}: PageProps<"/runs/[id]">): Promise<Metadata> {
  const { id } = await params;
  return { title: `Run ${id}` };
}

/**
 * A run's thread. Runs live behind the API client (the mock keeps created
 * runs in the browser), so the page itself fetches nothing on the server:
 * the client stream renders its own connecting / lost / error states.
 *
 * `RunStreamProvider` sits above the shell so a details panel passed as
 * `sidePanel` can read the same live state as the thread.
 */
export default async function RunPage({ params }: PageProps<"/runs/[id]">) {
  const { id } = await params;
  return (
    <RunStreamProvider runId={id}>
      <AppShell sidePanel={<RunSidePanel runId={id} />}>
        <RunThread runId={id} />
        <div className="sticky bottom-0 mt-auto pt-8">
          <NewRunComposer placeholder="Drop another file to start a new run" />
        </div>
      </AppShell>
    </RunStreamProvider>
  );
}
