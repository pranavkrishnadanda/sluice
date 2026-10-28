/**
 * The fixed, decorative colour-blob backdrop the glass panels refract.
 * Purely visual: hidden from assistive tech and pointer events.
 */
export function Backdrop() {
  return (
    <div aria-hidden="true" className="backdrop">
      <span className="blob blob-1" />
      <span className="blob blob-2" />
      <span className="blob blob-3" />
    </div>
  );
}
