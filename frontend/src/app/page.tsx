import { redirect } from "next/navigation";

/** Runs are the home screen. */
export default function Home(): never {
  redirect("/runs");
}
