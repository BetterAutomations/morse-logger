"""Expand CW / amateur-radio abbreviations for the human-readable log.

Design rules, in order of importance:

1. Never destroy the original. Default mode *glosses in place*:
       CQ CQ DE K4EIE K   ->   CQ CQ DE[from] K4EIE K[go ahead]
   so a wrong or unwanted expansion is always visible next to what was sent.
2. Say when you are unsure. Ambiguous tokens get a trailing '?' in the gloss
   (DE[from?]); confident ones do not (DE[from] between callsigns).
3. Never touch anything that looks like a callsign, a report or a number.
4. Cheap: one dict lookup per token, no regex over the dictionary, no
   third-party modules. Runs fine on a slow CPU with 4 GB RAM.

Modes: "gloss" (default), "replace" (expansion only), "off".
"""

import re

# ---------------------------------------------------------------- prosigns
# fldigi writes these as <XX> in the rx text stream.
PROSIGNS = {
    "AR": "end msg",
    "AS": "wait",
    "BK": "break in",
    "BT": "",                     # structural: the line break shows it
    "CL": "closing station",
    "HH": "correction",
    "KN": "go only",
    "SK": "sign off",
    "VE": "understood",
    "SOS": "distress",
}

# ---------------------------------------------------------------- glossary
# Kept deliberately conservative: only things a reader gains from. Values are
# shown in brackets, so they are written to be read, not to be parsed.
D = {
    # --- greetings / pleasantries
    "GM": "good morning", "GA": "good afternoon", "GE": "good evening",
    "GN": "good night", "GD": "good day", "GB": "goodbye", "GL": "good luck",
    "HI": "laugh", "DR": "dear", "OM": "op", "YL": "young lady",
    "XYL": "wife", "YF": "wife", "OB": "old boy", "LID": "poor op",
    "73": "best regards", "72": "best regards", "88": "love and kisses",
    "TKS": "thanks", "TNX": "thanks", "TU": "thank you", "SRI": "sorry",
    "PSE": "please", "CUL": "see you", "CUAGN": "see you again",
    # --- words that save keying
    "ABT": "about", "AGN": "again", "ANT": "antenna", "BCN": "beacon",
    "BN": "all between", "BTR": "better", "BD": "bad", "CFM": "confirm",
    "CK": "check", "CHK": "check", "CONDX": "conditions", "CPY": "copy",
    "CS": "callsign", "CUD": "could", "CUZ": "because", "ES": "and",
    "FB": "good", "FER": "for", "FREQ": "frequency",
    "GUD": "good", "GND": "ground", "HPE": "hope", "HR": "here", "HRD": "heard",
    "HV": "have", "HW": "how", "INFO": "information", "LIC": "licence",
    "LSN": "listen", "MNI": "many", "MNY": "many", "MSG": "message",
    "NIL": "nothing", "NR": "number", "NW": "now", "OP": "operator",
    "OPR": "operator", "PGM": "program", "PWR": "power", "RCVD": "received",
    "REF": "reference", "RIG": "equipment", "RPT": "repeat", "SA": "say",
    "SIG": "signal", "SN": "soon", "STN": "station", "TEMP": "temperature",
    "TX": "transmitter", "RX": "receiver", "TRX": "transceiver",
    "XCVR": "transceiver", "XMTR": "transmitter", "U": "you", "UR": "your",
    "VY": "very", "WID": "with", "WKD": "worked", "WKG": "working",
    "WL": "will", "WRD": "word", "WX": "weather", "WPM": "speed",
    "MOD": "mode", "AMP": "amplifier", "ATU": "antenna tuner",
    "BAT": "battery", "NCS": "net control", "NET": "net",
    "PLS": "please", "BURO": "bureau",
    # --- less common, but real: modes, traffic handling, weather, greetings
    "CW": "continuous wave", "DX": "distance", "SSB": "single sideband",
    "RTTY": "radioteletype", "VFO": "variable frequency oscillator",
    "RCVR": "receiver", "QSK": "full break-in", "QSX": "listening on",
    "TFC": "traffic", "SECT": "section", "ADR": "address", "HDL": "handle",
    "AL": "all", "CLD": "called", "DWN": "down", "FWD": "forward",
    "YR": "year", "WUD": "would", "TMW": "tomorrow", "BCNU": "be seeing you",
    "SWL": "shortwave listener", "HNY": "happy new year",
    "RA": "rain", "SNOW": "snow",
    "BK": "break", "CL": "closing", "HRD": "heard", "B4": "before", "LOG": "log",
    "CLDY": "cloudy", "AGN": "again", "CPI": "copy", "PADDLE": "paddle",
    "KEYER": "keyer", "BUG": "bug key",
    # --- reports and contest shorthand
    "RST": "signal report", "5NN": "599",
    # --- minimal but useful single letters (marked ambiguous below)
    "R": "received", "K": "go ahead", "DE": "from", "X": "bye", "W": "with",
    "SK": "sign off", "AR": "end msg", "KN": "go only",
}

# Q-signals are always safe to expand; none of them collide with words.
Q = {
    "QRG": "frequency", "QRH": "drifting",
    "QRL": "busy", "QRM": "interference",
    "QRN": "static", "QRO": "high power",
    "QRP": "low power", "QRQ": "send faster", "QRS": "send slower",
    "QRT": "closing", "QRU": "nothing for you",
    "QRV": "ready", "QRX": "stand by", "QRZ": "who's calling",
    "QSB": "fading", "QSL": "confirm", "QSA": "signal strength",
    "CQ": "all stations",
    "QSO": "contact", "QSP": "relay",
    "QSY": "retune", "QTC": "traffic",
    "QTH": "location", "QTR": "time",
}
D.update(Q)

# Tokens whose meaning depends on context; the gloss gets a '?' unless the
# context settles it.
AMBIGUOUS = {"R"}                    # the only token that still needs a flag
# DE is deliberately absent: it is the callsign separator, "from", in essentially
# every context. "Delaware" only appears in a state QSO party exchange, where the
# state is visible in the same line anyway.

PROSIGN_RE = re.compile(r"<([A-Z]{2,3})>")
# amateur callsign: optional prefix letters, digit, suffix letters
CALLSIGN_RE = re.compile(r"^(?:[A-Z]{1,2}\d[A-Z]{1,4}|\d[A-Z]\d?[A-Z]{1,4}|[A-Z]\d[A-Z]{1,4})$")
TRAIL = ".,?!;:"


def is_callsign(tok):
    return bool(CALLSIGN_RE.match(tok.upper()))


def _ctx_callsign(tokens, i):
    """True if a neighbouring token is a callsign (settles DE -> 'from')."""
    for j in (i - 1, i + 1):
        if 0 <= j < len(tokens) and is_callsign(tokens[j].strip(TRAIL)):
            return True
    return False


def gloss_token(tok, tokens, i):
    """Return (expansion, uncertain) for one token, or None to leave it alone.

    `uncertain` means the token has more than one plausible reading and the
    context test could not settle it - the annotation then carries a '?'.
    """
    core = tok.strip(TRAIL)
    if not core:
        return None
    pro = PROSIGN_RE.fullmatch(core)          # <BT>, <SK>, ...
    if pro:
        hint = PROSIGNS.get(pro.group(1))
        return (hint, False) if hint is not None else None   # "" is a real value
    key = core.upper()
    # an explicit glossary entry wins: "5NN" looks like a callsign to the regex
    if key not in D:
        return None                           # callsigns and plain numbers stay put
    if key in CONTEXT_PROSE:
        return CONTEXT_PROSE[key](tokens, i)
    return (D[key], key in AMBIGUOUS and not _confident(key, core, tokens, i))


def _prev_token(tokens, i):
    for j in range(i - 1, -1, -1):
        t = tokens[j].strip(TRAIL)
        if t:
            return t
    return ""


def _next_token(tokens, i):
    for j in range(i + 1, len(tokens)):
        t = tokens[j].strip(TRAIL)
        if t:
            return t
    return ""


END_MARKERS = {"K", "KN", "SK"}


def _ends_transmission(tokens, i):
    """True when only prosigns or a closing K/KN/SK follow."""
    rest = [t.strip(TRAIL) for t in tokens[i + 1:] if t.strip(TRAIL)]
    return not rest or all(PROSIGN_RE.fullmatch(t) or t.upper() in END_MARKERS
                           for t in rest)


def _k_context(tokens, i):
    """'K' invites the other station - unless it is a split callsign ("K 4EIE")."""
    nxt = _next_token(tokens, i)
    if nxt and any(ch.isdigit() for ch in nxt) and not PROSIGN_RE.fullmatch(nxt):
        return None                       # part of a callsign: leave it untouched
    return ("go ahead", not _ends_transmission(tokens, i))


def _x_context(tokens, i):
    """"X" is 'bye' in speech, but a multiplier between numbers - leave that alone."""
    if any(ch.isdigit() for ch in _neighbour(tokens, i)):
        return None
    return ("bye", not _ends_transmission(tokens, i))


# Tokens whose correct expansion depends on position, resolved instead of flagged.
# A callable may return None, meaning "do not annotate this token at all".
CONTEXT_PROSE = {
    "K": _k_context,
    "X": _x_context,
    # "100 W" is watts; a bare W between words is "with"
    "W": lambda tokens, i: (("watts", False)
                            if any(ch.isdigit() for ch in _prev_token(tokens, i))
                            else ("with", False)),
    # "GA" ending an over is "go ahead"; elsewhere it is a greeting
    "GA": lambda tokens, i: (("go ahead", False) if _ends_transmission(tokens, i)
                             else ("good afternoon", False)),
}


def _neighbour(tokens, i):
    for j in (i - 1, i + 1):
        if 0 <= j < len(tokens):
            t = tokens[j].strip(TRAIL)
            if t:
                return t
    return ""


def _confident(key, core, tokens, i):
    """Cheap context test. No model, no lookahead beyond one token."""
    if key == "DE":
        # "DE" between callsigns is 'from'; in a contest exchange it is Delaware
        return _ctx_callsign(tokens, i)
    if key == "K":
        # 'K' invites the other station when it ends the transmission
        return i == len(tokens) - 1 or tokens[i + 1].strip().startswith("<")
    if key == "R":
        # a bare R between letters is 'roger'; next to a report it is a digit run
        return not any(ch.isdigit() for ch in _neighbour(tokens, i))
    if key == "X":
        return i >= len(tokens) - 2 or tokens[i + 1].strip().startswith("<")
    return False        # W: 'with' or 'watts' - never settle it


def _render(text, mode="gloss", min_coherence=0.0, mark_uncertain=True):
    """Annotate decoded CW text and return (units, n_expansions). Returns (rendered, n_expansions).

    Notation is `word[TOKEN]`: the readable text first, the token that was sent
    in brackets, so a wrong expansion is visible right next to it. When the
    token has more than one plausible reading the bracket carries a '?', as in
    `from[DE?]` - the flag sits with the token because that is what is in doubt,
    not the English word.

    Lines below `min_coherence` are returned untouched: annotating a garbled
    decode only makes it harder to read.
    """
    if mode == "off" or not text:
        return (text.split(" ") if text else []), 0
    if min_coherence and coherence(text) < min_coherence:
        return (text.split(" ") if text else []), 0

    tokens = text.split(" ")
    out = []
    count = 0
    prev = None
    for i, tok in enumerate(tokens):
        got = gloss_token(tok, tokens, i)
        if got is None:
            out.append(tok)
            prev = None
            continue
        prose, uncertain = got
        core = tok.strip(TRAIL).upper()
        if prev is not None and core == prev:
            # "CQ CQ CQ" is one call: expanding every repetition buries the copy
            out.append(tok)
            continue
        prev = core
        count += 1
        tail = tok[len(tok.rstrip(TRAIL)):]
        core = tok[:len(tok) - len(tail)] if tail else tok
        if mode == "replace":
            out.append(f"{prose}{tail}")
        else:
            flag = "?" if (uncertain and mark_uncertain) else ""
            out.append(f"{prose}[{core}{flag}]{tail}")
    return out, count


WORDS = frozenset("""about above abt across after afternoon again agree alabama alaska all almost alone along already also although always among and another answer ant antenna antennas any anyone anything apart are arizona arkansas around ask asked away back bad band bands beach beam beautiful because been before behind being below best better between beyond big bit bits bought boy bring brother brought build built buy bye california call called calling callsign came can care carolina carry change check child children choose church city class clear close cloudy cold colorado come coming community company complete computer connecticut consider continue control cool cost could country county course cover cq create current cut dakota data daughter day days deal decide degrees delaware describe develop did die difference different difficult dipole direction discover distance do does doing done door down draw drive drop dry during early earth east eastern easy economic education effect eight either eleven else end energy english enough enter entire environment es especially even evening event ever every everyone everything evidence example expect experience explain eye face fact fall false family far fast father feel fer few field fifty figure fill final find fine finger finish finished fire first fish five floor florida follow food foot for force foreign form former forty forward four free frequency friend friends from front full fund future game garden gave general georgia get getting girl give given giving glass go goal goes going gone good goodbye got government grass great green ground group grow growth gud guess gun guy had hair half hampshire hand hang happen happy hard has hate have hawaii he head health hear heard heart heat heavy hello help helped her here herself hi high hill him himself his history hit hold home hope horse hospital hot hotel hour hours house how however hpe hr huge human hundred husband hw idaho idea identify if illinois image imagine impact important improve in include including increase indeed indiana individual industry information inside instead interest international interview into investment involve iowa island issue it item its itself jersey job join just kansas keep kentucky kept key kid kids kill kind kitchen knew know knowledge known lake land language large last late later laugh law lawyer lay lead leader learn learned least leave left leg legal less let letter level lie life light like liked likely line list listen listening little live local long look looked looking lose loss lot lots louisiana love loved low made main maine maintain major make makes making man manage manager many market marriage maryland massachusetts material matter may maybe me mean measure media medical meet meeting member memory men mention message meter meters method mexico michigan middle might military million mind minnesota minute minutes miss mission mississippi missouri mode model modern moment money montana month months more morning most mother mouth move movement movie much music must my myself name names nation national natural nature near nearly nebraska necessary need needed network nevada never new news newspaper next nice night nine no noise none nor north northern not note nothing notice now number occur off offer office officer official often oh ohio oil ok okay oklahoma old on once one only onto open operation opportunity option or order oregon organization other others our out outside over own owner page pain paint paper parent part participant particular particularly partner party pass past patient pattern pay peace pennsylvania people per perform performance perhaps period person personal phone physical pick picture piece place places plan plant play player please point police policy political poor popular population position positive possible power practice prepare present president pressure pretty prevent price private probably problem process produce product program project property protect prove provide pse public pull purpose push put qrm qsb qsl qso qth quality question quickly quite race radio rainy raise range rate rather reach read ready real reality realize really reason receive received recent recently recognize record red reduce reflect region relate relationship religious remain remember remove report represent require research resource respond response responsibility rest result return reveal rhode rich rig right rise risk river road rock role room rule run safe said same sang save saw say says scene school science score sea season seat second section security see seeing seek seem seen sell send senior sense sent series serious serve service set seven several shake shall share she shoot short should shoulder show side sign signal signals significant similar simple simply since sing single sister sit site situation six sixty size skill skin small smile snowy so social society soldier some somebody someone something sometimes son song soon sorry sort sound source south southern space speak special specific speech spend sport spring staff stage stand standard star start started state statement static station stay step still stock stop store story strategy street strong structure student study stuff style subject success such suddenly suffer suggest summer sunny support sure surface system table take taken taking talk talked talking task taught tax teach teacher team technology television tell temperature ten tend tennessee term test texas than thank thanks that the their them themselves then theory there these they thing things think third thirty this those though thought thousand threat three through throughout throw thus time times tnx to today together told tomorrow tonight too took top total tough toward tower towers town trade traditional training travel treat treatment tree trees trial tried trip trouble true truth try trying turn tv twelve twenty two type under understand unit until up upon ur us use used using usually utah value various vermont very victim view violence virginia visit voice vote wait waiting wake walk wall want wanted war warm was washington watch water watt watts way ways we weapon wear weather week weeks weight well went were west western wet what whatever when where whether which while white who whole whom whose why wide wife will win wind window windy wire wires wisconsin wish with within without woman women wonder woods word work worked worker working world worry worse worst would write writer wrong wrote wx wyoming yard yeah year years yes yesterday yet york you young your yourself""".split())


def translate(text, mode="gloss", min_coherence=0.0, mark_uncertain=True):
    """Annotate decoded CW text. Returns (rendered, n_expansions).

    Notation is `word[TOKEN]`: the readable text first, the token that was sent
    in brackets. See _render() for the rules.
    """
    units, count = _render(text, mode, min_coherence, mark_uncertain)
    return " ".join(units), count


def translate_units(text, mode="gloss", min_coherence=0.0, mark_uncertain=True):
    """Same as translate() but returns the atomic units, for safe wrapping.

    One unit is one unbreakable chunk of the rendered line: an expansion stays
    with its token ("all stations[CQ]"), because breaking between a meaning and
    the token it came from would defeat the point of the annotation.
    """
    return _render(text, mode, min_coherence, mark_uncertain)


def coherence(text):
    """0..1 - how believable a decoded line looks.

    Quality (q=) measures the *tone*; this measures the *text*. A loud signal
    with two stations overlapping, or a distorted stream, scores high on q and
    low here.

    Evidence that a token is real text: it is a prosign, a glossary term, a
    callsign, a short number (RST), or an ordinary English word. A four-letter
    run of letters is *not* evidence - that is exactly what a mis-decoded fist
    produces ("SANG", "DRIE", "ITAC"), so shape alone would score garbage high.
    """
    toks = [t for t in text.split() if t.strip(TRAIL)]
    if not toks:
        return 0.0
    good = 0
    for t in toks:
        core = t.strip(TRAIL)
        up = core.upper()
        low = core.lower()
        if PROSIGN_RE.fullmatch(t):
            good += 1
        elif up in D:
            good += 1
        elif CALLSIGN_RE.match(up):
            good += 1
        elif core.isdigit() and len(core) <= 3:
            good += 1
        elif low in WORDS:
            good += 1
        elif up in ("I", "A"):
            good += 1
    return round(good / len(toks), 2)


def legend():
    """A short key for the top of a log file."""
    return ("Legend: word[TOKEN] = CW abbreviation expanded, original token in "
            "brackets; TOKEN? = the reading is context-dependent, check the token; "
            "a line ending in [<BT>] is where the sender broke the paragraph - the "
            "next line is a new one.")
