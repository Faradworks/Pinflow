# Vendored KiCanvas bundle

`kicanvas.js` is the upstream KiCanvas bundle with one small in-place patch.
Don't hand-edit beyond that — refresh by re-downloading and re-applying the
patch via `apply-patches.sh`.

- Upstream: https://kicanvas.org/kicanvas/kicanvas.js
- Project: https://github.com/theacodes/kicanvas (MIT, (c) Alethea Katherine Flowers)
- Upstream fetched: 2026-05-13
- Upstream sha256: `ca910f25276c3efb9aacb3a5d6341d4d9af4736d4c875fb0440d2cc856865ab7`
- Patched sha256:  `f2feb36c2b3565ad83317723945ce7afcccf4284ab37903dea501c749673abad`

Refresh + re-patch (from repo root):

```sh
curl -fL -o apps/desktop/public/kicanvas/kicanvas.js https://kicanvas.org/kicanvas/kicanvas.js
curl -fL -o apps/desktop/public/kicanvas/LICENSE     https://raw.githubusercontent.com/theacodes/kicanvas/main/LICENSE.md
apps/desktop/public/kicanvas/apply-patches.sh
shasum -a 256 apps/desktop/public/kicanvas/kicanvas.js   # should match Patched sha256
```

Update the two sha256 lines above after refreshing.

## Local patches

Each patch is applied by `apply-patches.sh` as an exact-string replacement;
new KiCanvas builds will re-introduce the original string and need re-patching.

### 1. Accept `(exclude_from_sim ...)` on top-level schematic `(text …)` forms

KiCad 8+ added `(exclude_from_sim no)` to schematic-side text annotations.
KiCanvas's bundled parser (KiCad-6 era) registers the field on lib symbols
and symbol instances but not on the top-level `(text …)` form, so it logs
`No definition found for element exclude_from_sim,no …` and skips the
element. Our agent emits `(text …)` annotations for block-frame labels, so
unpatched KiCanvas drops them.

Find: `a.start("text"),a.positional("text"),a.item("at",K),a.item("effects",G),a.pair("uuid",R.string))`
Replace with the same string plus `,a.pair("exclude_from_sim",R.boolean)` before the closing paren.

### 2. Skip — rather than crash — on a symbol with no matching `(lib_symbols ...)` entry

KiCanvas has no on-disk symbol resolution: it can only render what's
embedded in the file's `(lib_symbols ...)` block. The agent's edit pipeline
sometimes adds a `(symbol (lib_id "power:VCC") ...)` (or other new lib_id)
without merging the corresponding `lib_symbols` entry — KiCad opens the
file fine because it resolves from disk, but in KiCanvas
`SchematicSymbolPainter.paint` dereferences `r.lib_symbol.power` on a
`SchematicSymbol` whose `lib_symbol` getter returned `undefined`, throws,
breaks out of `paint_layer`'s `for` loop, and the entire schematic stops
rendering — the canvas goes black. The fix early-returns from `paint` on a
missing lib_symbol (logging once per render in the `:Symbol:Foreground`
pass) so the surrounding schematic still draws and the broken symbol shows
as a hole instead of a wipeout.

Find: `paint(t,r){if(t.name==":Interactive"&&r.lib_symbol.power)return;`
Replace with: `paint(t,r){if(!r.lib_symbol){if(t.name==":Symbol:Foreground")console.warn("kicanvas: missing lib_symbols entry for",r.lib_name||r.lib_id);return;}if(t.name==":Interactive"&&r.lib_symbol.power)return;`

### 3. Don't crash parsing KiCad-7+ symbol instances (no `(default_instance …)`)

KiCad 6 stored a `(default_instance (reference …) (unit …) (value …) (footprint …))`
element on each schematic symbol; KiCanvas's `SchematicSymbol` constructor reads it to
backfill the symbol's `Value`/`Footprint` text when those properties are absent. KiCad 7+
removed `(default_instance …)` in favour of the per-sheet `(instances …)` block, so on any
modern file `this.default_instance` is `undefined`. The constructor then throws
`TypeError: Cannot read properties of undefined (reading 'value')` on the first symbol with
no explicit `Value` property (a GND power symbol is the usual trigger), which aborts the whole
parse in `new KicadSch` and leaves the canvas blank. The fix guards both `default_instance`
reads — the `(property …)` blocks already carry the display values, so the backfill is a no-op
on modern files anyway.

Find: `this.get_property_text("Value")==null&&this.set_property_text("Value",this.default_instance.value),!this.get_property_text("Footprint")==null&&this.set_property_text("Footprint",this.default_instance.footprint)`
Replace with: `this.get_property_text("Value")==null&&this.default_instance&&this.set_property_text("Value",this.default_instance.value),!this.get_property_text("Footprint")==null&&this.default_instance&&this.set_property_text("Footprint",this.default_instance.footprint)`

The bundle is loaded via `<script type="module" src="/kicanvas/kicanvas.js">` from `apps/desktop/index.html` (Vite serves `public/` at the root). It registers `<kicanvas-embed>` and `<kicanvas-source>` as global custom elements; see `apps/desktop/src/components/schematic/SchematicView.tsx`.

Requires WebGL2 in the host webview — fine on macOS WKWebView 13.3+ and any current Windows WebView2 Evergreen runtime.
