#!/usr/bin/env python3
"""Kontrola duplicitných deklarácií v JavaScripte bez prehliadača a bez Node.

Podnet: po zlúčení sa v stats_ui.js ocitli dvakrát rovnaké `const` deklarácie. Syntaktická kontrola to nezachytila,
no prehliadač skript odmietne (SyntaxError: Identifier ... has already been declared) a záložka Štatistiky sa nenačíta.

Kontroluje stats_ui.js, sw.js a každý inline <script> v ui.html a monitor/index.html. Pre každý blok `{ }` (vrátane tela
funkcie / IIFE) hľadá opakované const / let / class / function s rovnakým menom. Inline skripty jednej stránky zdieľajú
globálny rozsah, preto sa ich najvyššia úroveň kontroluje spolu. Heuristický tokenizér (reťazce, šablóny, regulárne
výrazy a komentáre preskakuje); `var` sa nekontroluje (opakovanie je legálne).
Spustenie: python3 -X utf8 test_js_deklaracie.py"""
import os, re, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.abspath(__file__))
DECL = ("const", "let", "class", "function")
# po týchto kľúčových slovách môže nasledovať regulárny výraz (nie delenie)
RE_PREV_KW = {"return", "typeof", "case", "in", "of", "delete", "void", "throw", "new", "else", "do", "instanceof", "yield", "await"}
ID_START = re.compile(r"[A-Za-z_$ -￿]")
ID_PART = re.compile(r"[A-Za-z0-9_$ -￿]")


def tokenize(src):
    """Zoznam (druh, text, riadok); druh: 'id' | 'p' (interpunkcia) | 'x' (číslo, reťazec, šablóna, regex)."""
    toks, i, n, line = [], 0, len(src), 1

    def skip_str(i, q):
        while i < n and src[i] != q:
            i += 2 if src[i] == "\\" else 1
        return i + 1

    def skip_tpl(i):   # i je za otváracím `
        nonlocal line
        while i < n and src[i] != "`":
            if src[i] == "\\":
                i += 2
            elif src[i] == "$" and src[i + 1:i + 2] == "{":
                i = skip_expr(i + 2)
            else:
                line += src[i] == "\n"
                i += 1
        return i + 1

    def skip_expr(i):   # i je za `${`; vráti pozíciu za zodpovedajúcou `}`
        nonlocal line
        depth = 1
        while i < n and depth:
            ch = src[i]
            if ch in "'\"":
                i = skip_str(i + 1, ch)
                continue
            if ch == "`":
                i = skip_tpl(i + 1)
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            line += ch == "\n"
            i += 1
        return i

    while i < n:
        ch = src[i]
        if ch == "\n":
            line += 1
            i += 1
        elif ch in " \t\r":
            i += 1
        elif src.startswith("//", i):
            while i < n and src[i] != "\n":
                i += 1
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            line += src.count("\n", i, j)
            i = j
        elif ch in "'\"":
            toks.append(("x", "str", line))
            i = skip_str(i + 1, ch)
        elif ch == "`":
            l0 = line
            i = skip_tpl(i + 1)
            toks.append(("x", "tpl", l0))
        elif ch.isdigit() or (ch == "." and src[i + 1:i + 2].isdigit()):
            j = i + 1
            while j < n and re.match(r"[0-9A-Za-z_.]", src[j]):
                if src[j] in "eE" and src[j + 1:j + 2] in ("+", "-"):
                    j += 1
                j += 1
            toks.append(("x", "num", line))
            i = j
        elif ID_START.match(ch):
            j = i + 1
            while j < n and ID_PART.match(src[j]):
                j += 1
            toks.append(("id", src[i:j], line))
            i = j
        elif ch == "/":
            prev = toks[-1] if toks else None
            is_re = (prev is None or (prev[0] == "p" and prev[1] not in ")]}") or (prev[0] == "id" and prev[1] in RE_PREV_KW))
            if is_re:
                j, cls = i + 1, False
                while j < n and (src[j] != "/" or cls) and src[j] != "\n":
                    if src[j] == "\\":
                        j += 1
                    elif src[j] == "[":
                        cls = True
                    elif src[j] == "]":
                        cls = False
                    j += 1
                j += 1
                while j < n and ID_PART.match(src[j]):
                    j += 1
                toks.append(("x", "re", line))
                i = j
            else:
                toks.append(("p", "/", line))
                i += 1
        else:
            toks.append(("p", ch, line))
            i += 1
    return toks


def _pattern_names(toks, i):
    """Názvy z destrukturovacieho vzoru od `{`/`[` na pozícii i. Vráti (názvy, index za vzorom)."""
    names, depth, j = [], 0, i
    close = {"{": "}", "[": "]"}
    start = j
    while j < len(toks):
        k, t, _ = toks[j]
        if k == "p" and t in "{[(":
            depth += 1
        elif k == "p" and t in "}])":
            depth -= 1
            if depth == 0:
                j += 1
                break
        elif k == "id" and depth >= 1:
            prev, nxt = toks[j - 1], toks[j + 1] if j + 1 < len(toks) else ("p", "", 0)
            in_obj = _enclosing(toks, start, j) == "{"
            # {a: b} -> b je názov, a je kľúč; {a = 1}, {a}, [a, b] -> a je názov; hodnoty po `=` sa preskakujú
            if in_obj and nxt[0] == "p" and nxt[1] == ":":
                pass
            elif prev[0] == "p" and prev[1] in "{[,:" or (prev[0] == "p" and prev[1] == "." ):
                names.append((t, toks[j][2]))
        j += 1
    return names, j


def _enclosing(toks, start, j):
    """Najbližšia nezatvorená zátvorka pred pozíciou j (v rámci vzoru od start)."""
    st = []
    for k in range(start, j):
        if toks[k][0] == "p":
            if toks[k][1] in "{[(":
                st.append(toks[k][1])
            elif toks[k][1] in "}])" and st:
                st.pop()
    return st[-1] if st else ""


def find_duplicates(src, scope_state=None):
    """Zoznam (meno, riadok prvej deklarácie, riadok duplicity). scope_state: zdieľaný slovník globálneho rozsahu
    (viac inline skriptov jednej stránky); inak sa používa nový."""
    toks = tokenize(src)
    top = scope_state if scope_state is not None else {}
    frames = [{"names": top, "paren": 0}]
    dups = []
    active = None   # frame, v ktorom prebieha const/let so zoznamom `a = 1, b = 2`

    def declare(name, line):
        names = frames[-1]["names"]
        if name in names:
            dups.append((name, names[name], line))
        else:
            names[name] = line

    i, n = 0, len(toks)
    while i < n:
        k, t, line = toks[i]
        top_f = frames[-1]
        if k == "p":
            if t == "{":
                frames.append({"names": {}, "paren": 0})
                active = None
            elif t == "}":
                if len(frames) > 1:
                    frames.pop()
                active = None
            elif t in "([":
                top_f["paren"] += 1
            elif t in ")]":
                top_f["paren"] = max(0, top_f["paren"] - 1)
            elif t == ";" and top_f["paren"] == 0:
                active = None
            elif t == "," and active is top_f and top_f["paren"] == 0:
                nk = toks[i + 1] if i + 1 < n else None
                if nk and nk[0] == "id":
                    declare(nk[1], nk[2])
                    i += 1
                elif nk and nk[0] == "p" and nk[1] in "{[":
                    names, j = _pattern_names(toks, i + 1)
                    for nm, ln in names:
                        declare(nm, ln)
                    i = j - 1
            i += 1
            continue
        if k == "id" and t in DECL and top_f["paren"] == 0:
            prev = toks[i - 1] if i else None
            at_start = prev is None or (prev[0] == "p" and prev[1] in ";{}") or (prev[0] == "id" and prev[1] in ("async", "export")) \
                or prev[2] < line   # nový riadok (ASI)
            nx = toks[i + 1] if i + 1 < n else None
            prev_ok = prev is None or not (prev[0] == "p" and prev[1] in ".=:,(?&|!+-*<>[")   # `x.let`, `a = function f`, kľúč objektu
            if at_start and prev_ok and nx:
                if t == "function" and nx[0] == "p" and nx[1] == "*":
                    i += 1
                    nx = toks[i + 1]
                if nx[0] == "id" and not (t in ("const", "let") and nx[1] in ("in", "of")):
                    declare(nx[1], nx[2])
                    if t in ("const", "let"):
                        active = frames[-1]
                    i += 2
                    continue
                if t in ("const", "let") and nx[0] == "p" and nx[1] in "{[":
                    names, j = _pattern_names(toks, i + 1)
                    for nm, ln in names:
                        declare(nm, ln)
                    active = frames[-1]
                    i = j
                    continue
        i += 1
    return dups


def html_scripts(html):
    """[(číslo prvého riadku, kód)] klasických inline skriptov (bez src= a bez type typu application/json apod.)."""
    out = []
    for m in re.finditer(r"<script\b([^>]*)>([\s\S]*?)</script\s*>", html, re.I):
        attrs = m.group(1)
        if re.search(r"\bsrc\s*=", attrs, re.I):
            continue
        tm = re.search(r"\btype\s*=\s*[\"']?([^\"'\s>]*)", attrs, re.I)
        if tm and tm.group(1).lower() not in ("", "text/javascript", "application/javascript"):
            continue
        out.append((html.count("\n", 0, m.start(2)) + 1, m.group(2)))
    return out


def check_js(src, name):
    return ["%s (riadok %d): `%s` je už deklarované na riadku %d" % (name, b, nm, a) for nm, a, b in find_duplicates(src)]


def check_html(html, name):
    errs, shared = [], {}
    for first, code in html_scripts(html):
        for nm, a, b in find_duplicates(code, shared):
            errs.append("%s (riadok %d): `%s` je už deklarované (riadok %d)" % (name, first + b - 1, nm, first + a - 1 if a else 0))
    return errs


def check_repo(root=ROOT):
    errs = []
    for rel in ("stats_ui.js", "sw.js"):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            errs += check_js(f.read(), rel)
    for rel in ("ui.html", "monitor/index.html"):
        with open(os.path.join(root, rel), encoding="utf-8") as f:
            errs += check_html(f.read(), rel)
    return errs


class Tokenizer(unittest.TestCase):
    def test_zaklad(self):
        self.assertEqual(find_duplicates("const a = 1;\nconst b = 2;\nlet c;\nfunction d() {}\nclass E {}"), [])
        self.assertEqual(find_duplicates("const a = 1;\nconst a = 2;"), [("a", 1, 2)])
        self.assertEqual(find_duplicates("let a;\nconst a = 1;")[0][0], "a")
        self.assertEqual(find_duplicates("function f(){}\nfunction f(){}")[0][0], "f")
        self.assertEqual(find_duplicates("class A{}\nconst A = 1")[0][0], "A")

    def test_rozsahy(self):
        self.assertEqual(find_duplicates("const a=1;\nfunction f(){ const a=2; }\nfunction g(){ const a=3; }"), [])
        self.assertEqual(find_duplicates("(function(){\n'use strict';\nconst a=1;\nif(x){ const a=2; }\nconst a=3;\n})()")[0][0], "a")
        self.assertEqual(find_duplicates("var a=1;\nvar a=2;"), [])
        self.assertEqual(find_duplicates("for (const i of x) {}\nfor (const i of y) {}\nfor (let j=0;j<2;j++){}\nfor (let j=0;;){}"), [])

    def test_retazce_komentare_regex(self):
        src = "// const a = 1\n/* const a = 2 */\nconst s = 'const a = 1; const a = 2';\nconst t = `x ${ `const a=1` } const a = 1\nconst a = 2`;\nconst r = /const a = 1/g;\nconst a = 5;"
        self.assertEqual(find_duplicates(src), [])
        self.assertEqual(find_duplicates("const x = a / 2;\nconst y = b / 3;\nconst x = 1;")[0][0], "x")

    def test_zoznam_a_destrukturovanie(self):
        self.assertEqual(find_duplicates("const a = 1, b = f(1, 2), c = 3;\nconst b = 2;")[0][0], "b")
        self.assertEqual(find_duplicates("const {a, b: c, d = 1} = o;\nconst c = 1;")[0][0], "c")
        self.assertEqual(find_duplicates("const {a, b: c} = o;\nconst b = 1;"), [])   # b je len kľúč
        self.assertEqual(find_duplicates("const [a, , b] = o;\nlet a;")[0][0], "a")
        self.assertEqual(find_duplicates("const o = {function: 1, let: 2};\nconst p = {function: 1, let: 2};"), [])
        self.assertEqual(find_duplicates("const f = function g() {};\nconst h = function g() {};"), [])

    def test_viac_skriptov_zdielaju_globalny_rozsah(self):
        html = "<script>const a=1;</script><script src='x.js'></script><script type='application/json'>const a=1</script><script>const a=2;</script>"
        self.assertEqual(len(check_html(html, "t.html")), 1)


class Repozitar(unittest.TestCase):
    def test_sucasne_subory_su_bez_duplicit(self):
        self.assertEqual(check_repo(), [])

    def test_vlozeny_duplikat_sa_najde(self):
        """Do dočasnej kópie sa vloží duplicitná deklarácia (ako po nevydarenom merge) - kontrola ju musí nájsť."""
        with open(os.path.join(ROOT, "stats_ui.js"), encoding="utf-8") as f:
            js = f.read()
        m = re.search(r"^const (\w+) = .*$", js, re.M)
        self.assertTrue(m)
        bad = js.replace(m.group(0), m.group(0) + "\n" + m.group(0), 1)
        errs = check_js(bad, "stats_ui.js")
        self.assertEqual(len(errs), 1, errs)
        self.assertIn("`%s`" % m.group(1), errs[0])
        with open(os.path.join(ROOT, "ui.html"), encoding="utf-8") as f:
            html = f.read()
        # duplicitná deklarácia v inline skripte ui.html
        mm = re.search(r"^const (\w+) = .*$", html, re.M)
        self.assertTrue(mm)
        bad_html = html.replace(mm.group(0), mm.group(0) + "\n" + mm.group(0), 1)
        errs = check_html(bad_html, "ui.html")
        self.assertEqual(len(errs), 1, errs)
        with tempfile.TemporaryDirectory() as d:   # celá kópia súborov + check_repo
            for rel in ("stats_ui.js", "sw.js", "ui.html", "monitor/index.html"):
                os.makedirs(os.path.dirname(os.path.join(d, rel)), exist_ok=True)
                with open(os.path.join(ROOT, rel), encoding="utf-8") as f, open(os.path.join(d, rel), "w", encoding="utf-8") as g:
                    g.write(f.read() if rel != "stats_ui.js" else bad)
            self.assertEqual(len(check_repo(d)), 1)


if __name__ == "__main__":
    unittest.main(verbosity=1)
