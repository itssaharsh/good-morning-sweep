"""Words that mark a picture as made to be forwarded. A greeting is set aside only when the model read one of these
on it (or it arrived more than once). Indian languages first, because that is where the flood comes from."""
import re

FORWARD_WORDS = [
    # English
    r"good\s*morning", r"good\s*night", r"good\s*evening", r"good\s*afternoon", r"\bgm\b", r"\bgn\b",
    r"have\s+a\s+(nice|great|good|blessed|wonderful|happy)", r"\bblessed\b", r"\bblessings?\b", r"\bwish(es)?\b",
    r"\bhappy\b", r"\bgreetings?\b", r"\bsunday\b", r"\bmonday\b", r"\btuesday\b", r"\bwednesday\b", r"\bthursday\b",
    r"\bfriday\b", r"\bsaturday\b", r"\bweekend\b", r"\bdiwali\b", r"\bdeepavali\b", r"\bholi\b", r"\bnavratri\b",
    r"\bdussehra\b", r"\bdurga\b", r"\bganesh\b", r"\bchaturthi\b", r"\beid\b", r"\bchristmas\b", r"new\s*year",
    r"\bpongal\b", r"\bonam\b", r"\blohri\b", r"\bbaisakhi\b", r"\braksha\b", r"\bjanmashtami\b", r"\bmakar\b",
    r"\bsankranti\b", r"\bkarva\b", r"\bdhanteras\b", r"\bbhai\s*dooj\b", r"\bfestival\b", r"\bgod\b", r"\blord\b",
    r"\bprayer\b", r"\bsmile\b", r"\blife\b", r"\bthought\s+of\s+the\s+day\b", r"\bquote\b", r"\bmotivation",
    r"\bsuccess\b", r"\bforwarded\b", r"\bshare\b", r"\bviral\b", r"\bbreaking\b", r"\balert\b", r"\boffer\b",
    r"\bsale\b", r"\bdiscount\b", r"\bfree\b", r"\bjoke\b", r"\blol\b", r"\bnamaste\b", r"\bnamaskar\b",
    r"\bsweet\s+dreams\b", r"\bstay\s+(safe|happy|blessed)\b", r"\bfriendship\b", r"\bmorning\b", r"\bnight\b",
    # Hindi and Hinglish in Latin letters
    r"\bshubh\b", r"\bsubh\b", r"\bsuprabhat\b", r"\bprabhat\b", r"\bratri\b", r"\bjai\b", r"\bjay\b", r"\bshri\b",
    r"\bshree\b", r"\bradhe\b", r"\bkrishna\b", r"\bram\b", r"\bhanuman\b", r"\bmahadev\b", r"\bshiv\b",
    r"\bsai\b", r"\bbhagwan\b", r"\bmata\b", r"\bdin\b", r"\bsandesh\b",
    # Devanagari (Hindi, Marathi)
    r"शुभ", r"सुप्रभात", r"प्रभात", r"रात्रि", r"गुड", r"मॉर्निंग", r"नाईट", r"जय", r"श्री", r"राधे", r"कृष्ण", r"राम",
    r"हनुमान", r"महादेव", r"शिव", r"गणेश", r"दिवाली", r"दीपावली", r"होली", r"नवरात्रि", r"शुभकामनाएं", r"शुभकामना",
    r"बधाई", r"सुविचार", r"नमस्ते", r"नमस्कार", r"आपका\s*दिन", r"मंगलमय", r"सोमवार", r"मंगलवार", r"बुधवार",
    r"गुरुवार", r"शुक्रवार", r"शनिवार", r"रविवार", r"ॐ",
    # Other Indian scripts: good morning / blessings
    r"সুপ্রভাত", r"শুভ", r"સુપ્રભાત", r"શુભ", r"ਸ਼ੁਭ", r"ਸਤਿ", r"காலை\s*வணக்கம்", r"இனிய", r"శుభోదయం", r"శుభ",
    r"ಶುಭೋದಯ", r"ಶುಭ", r"ശുഭ", r"സുപ്രഭാതം", r"ଶୁଭ",
]
_RX = re.compile("|".join(FORWARD_WORDS), re.I)


def forward_words(text):
    """The forward words found in the text (empty when none)."""
    return sorted({m.group(0).lower() for m in _RX.finditer(text or "")})


def has_words(text):
    """True when the model really quoted something: two Latin words, or four letters of another script."""
    t = (text or "").strip()
    if not t:
        return False
    latin = re.findall(r"[A-Za-z]{2,}", t)
    other = re.findall(r"[^\x00-\x7F\s\d\W]", t)
    return len(latin) >= 2 or len(other) >= 4 or (len(latin) >= 1 and len(other) >= 2)
