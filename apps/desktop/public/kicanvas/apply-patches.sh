#!/usr/bin/env bash
# Re-apply local KiCanvas patches after a bundle refresh.
# See README.md for what each patch does and why.
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
file="$dir/kicanvas.js"

if [[ ! -f "$file" ]]; then
  echo "kicanvas.js not found at $file" >&2
  exit 1
fi

python3 - "$file" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
patches = [
    # 1. Accept (exclude_from_sim ...) on top-level schematic (text ...) forms.
    (
        'a.start("text"),a.positional("text"),a.item("at",K),a.item("effects",G),a.pair("uuid",R.string))',
        'a.start("text"),a.positional("text"),a.item("at",K),a.item("effects",G),a.pair("uuid",R.string),a.pair("exclude_from_sim",R.boolean))',
    ),
    # 2. Skip — rather than crash — when a (symbol ...) instance references a
    #    lib_id with no matching entry in (lib_symbols ...). The painter
    #    propagates the TypeError up through paint_layer, breaking the rest
    #    of the schematic render and leaving the canvas black.
    (
        'paint(t,r){if(t.name==":Interactive"&&r.lib_symbol.power)return;',
        'paint(t,r){if(!r.lib_symbol){if(t.name==":Symbol:Foreground")console.warn("kicanvas: missing lib_symbols entry for",r.lib_name||r.lib_id);return;}if(t.name==":Interactive"&&r.lib_symbol.power)return;',
    ),
    # 3. Don't crash parsing KiCad-7+ symbol instances. KiCad 6 stored a
    #    (default_instance ...) element this parser reads to backfill a
    #    symbol's Value/Footprint when those properties are absent. KiCad 7+
    #    replaced it with the (instances ...) block, so `this.default_instance`
    #    is undefined on every modern file — and the SchematicSymbol ctor then
    #    throws "Cannot read properties of undefined (reading 'value')" on the
    #    first symbol with no explicit Value property (a GND power symbol is the
    #    usual trigger), aborting the whole parse and leaving the canvas blank.
    #    Guard both default_instance reads.
    (
        'this.get_property_text("Value")==null&&this.set_property_text("Value",this.default_instance.value),!this.get_property_text("Footprint")==null&&this.set_property_text("Footprint",this.default_instance.footprint)',
        'this.get_property_text("Value")==null&&this.default_instance&&this.set_property_text("Value",this.default_instance.value),!this.get_property_text("Footprint")==null&&this.default_instance&&this.set_property_text("Footprint",this.default_instance.footprint)',
    ),
    # 4. Drop the "Help" activity from the viewer side-bar. make_post_activities()
    #    returns [Preferences, Help]; the Help panel is upstream-branding noise in
    #    an embedded context and has no controlslist flag to disable it (unlike the
    #    download button, which we turn off via controlslist="nodownload"). Remove
    #    the second array element, leaving Preferences.
    (
        'make_post_activities(){return[f`<kc-ui-activity\n'
        '                slot="activities"\n'
        '                name="Preferences"\n'
        '                icon="settings"\n'
        '                button-location="bottom">\n'
        '                <kc-preferences-panel></kc-preferences-panel>\n'
        '            </kc-ui-activity>`,f` <kc-ui-activity\n'
        '                slot="activities"\n'
        '                name="Help"\n'
        '                icon="help"\n'
        '                button-location="bottom">\n'
        '                <kc-help-panel></kc-help-panel>\n'
        '            </kc-ui-activity>`]}',
        'make_post_activities(){return[f`<kc-ui-activity\n'
        '                slot="activities"\n'
        '                name="Preferences"\n'
        '                icon="settings"\n'
        '                button-location="bottom">\n'
        '                <kc-preferences-panel></kc-preferences-panel>\n'
        '            </kc-ui-activity>`]}',
    ),
]
applied = []
for i, (old, new) in enumerate(patches, start=1):
    if new in text:
        applied.append(f"#{i} already applied")
        continue
    count = text.count(old)
    if count != 1:
        print(f"patch #{i}: expected exactly 1 match of original, found {count}; aborting", file=sys.stderr)
        sys.exit(2)
    text = text.replace(old, new)
    applied.append(f"#{i} applied")

path.write_text(text)
for line in applied:
    print(line)
PY

shasum -a 256 "$file"
