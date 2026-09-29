"""The scam and bot rule catalog, built from documented tactics.

Every rule has a plain-language reason, a strength, and the channels it applies
to. Text is normalised first, because bots hide from filters with lookalike
letters, zero-width characters and split links ("bigfollows*com",
"s t r e a m b o o . com"). Rules then run on both the raw and the cleaned text.

Strengths map to signal weights:
    weak 0.2 | medium 0.4 | strong 0.65 | near_certain 0.9

Sources and evidence are in docs/detection-rules.md. Thresholds that no source
measured are marked there as our own calibration, tuned against the test
networks and the false-positive cases in tests/.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Optional
from urllib.parse import urlparse

STRENGTH = {"weak": 0.2, "medium": 0.4, "strong": 0.65, "near_certain": 0.9}
ALL = frozenset({"dm", "sms", "comment", "live", "email"})
SOCIAL = frozenset({"dm", "comment", "live"})
PRIVATE = frozenset({"dm", "sms"})

# ---------------------------------------------------------------------------------
# Normalisation

_ZERO_WIDTH = re.compile("[​-‏‪-‮⁠-⁤﻿᠎͏︀-️]|[\U000e0000-\U000e007f]")
_CONFUSABLE = str.maketrans({
    # Cyrillic and Greek letters that look Latin
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "х": "x", "у": "y", "і": "i", "ј": "j", "ѕ": "s", "һ": "h",
    "А": "a", "В": "b", "Е": "e", "К": "k", "М": "m", "Н": "h", "О": "o", "Р": "p", "С": "c", "Т": "t", "Х": "x",
    "ο": "o", "α": "a", "ν": "v", "τ": "t", "ι": "i", "κ": "k", "ρ": "p", "υ": "u",
})
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s"})
_SPLIT_LINK = re.compile(r"(?<=[a-z0-9])\s*(?:[.*·•,]|\(\s*(?:dot|d0t)\s*\)|\[\s*(?:dot|d0t)\s*\]|\s(?:dot|d0t)\s)\s*(?=(?:com|net|org|gg|tv|io|xyz|live|me|ly|link|shop|top|site|online|store|click|ru|cc)\b)")
_SPACED = re.compile(r"\b(?:\w[\s.\-_*]){4,}\w\b")
_TLD_URL = re.compile(r"\b[a-z0-9-]{2,63}\.(?:com|net|org|gg|tv|io|xyz|live|me|ly|link|shop|top|site|online|store|click|ru|cc)\b")


def _mixed_script(token: str) -> bool:
    scripts = set()
    for ch in token:
        if ch.isalpha():
            name = unicodedata.name(ch, "")
            scripts.add("CYRILLIC" if "CYRILLIC" in name else "GREEK" if "GREEK" in name else "LATIN" if "LATIN" in name else "OTHER")
    return "LATIN" in scripts and bool(scripts & {"CYRILLIC", "GREEK"})


@dataclass
class Clean:
    raw: str
    text: str                  # NFKC, casefolded, zero-width and accents stripped, lookalikes mapped
    leet: str                  # text with leetspeak folded (for lexicon checks only)
    obfuscated_link: bool      # a domain only appeared after de-obfuscation
    mixed_script: bool         # a word mixing Latin with Cyrillic/Greek letters
    zalgo: bool                # heavy stacked accents


def normalize(raw: str) -> Clean:
    t = unicodedata.normalize("NFKC", raw or "")
    t = _ZERO_WIDTH.sub("", t)
    mixed = any(_mixed_script(tok) for tok in t.split())
    decomposed = unicodedata.normalize("NFD", t)
    marks = sum(1 for ch in decomposed if unicodedata.combining(ch))
    t = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    t = t.translate(_CONFUSABLE).casefold()
    t = " ".join(t.split())
    had_domain = bool(_TLD_URL.search(t))
    joined = _SPLIT_LINK.sub(".", t)
    joined = _SPACED.sub(lambda m: re.sub(r"[\s.\-_*]", "", m.group(0)) if len(m.group(0)) >= 9 else m.group(0), joined)
    obf = not had_domain and bool(_TLD_URL.search(joined))
    return Clean(raw=raw or "", text=joined, leet=joined.translate(_LEET), obfuscated_link=obf, mixed_script=mixed,
                 zalgo=marks > 10)


# ---------------------------------------------------------------------------------
# Rules

URL = re.compile(r"(https?://[^\s<>\"']+|www\.[^\s<>\"']+|\b(?:bit\.ly|tinyurl\.com|t\.co|is\.gd|cutt\.ly|rb\.gy|linktr\.ee|t\.me|wa\.me)/\S+)", re.I)
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "is.gd", "cutt.ly", "rb.gy", "shorturl.at", "ow.ly", "buff.ly", "tiny.cc", "rebrand.ly"}
CHEAP_TLDS = {"top", "xyz", "icu", "xin", "cc", "vip", "shop", "win", "lat", "bond", "sbs", "cfd", "click", "online", "site", "live", "buzz", "rest", "cyou"}
BRAND_WORDS = ("usps", "ezpass", "e-zpass", "fastrak", "sunpass", "toll", "dmv", "irs", "apple", "icloud", "amazon", "paypal",
               "netflix", "chase", "wellsfargo", "bankofamerica", "coinbase", "fedex", "ups", "dhl", "instagram", "facebook", "meta", "tiktok")
REAL_BRAND_DOMAINS = {"usps.com", "irs.gov", "apple.com", "icloud.com", "amazon.com", "paypal.com", "netflix.com", "chase.com",
                      "wellsfargo.com", "bankofamerica.com", "coinbase.com", "fedex.com", "ups.com", "dhl.com", "instagram.com",
                      "facebook.com", "meta.com", "tiktok.com", "e-zpassny.com", "ezpassnj.com", "ezpassva.com", "sunpass.com",
                      "bayareafastrak.org", "thetollroads.com", "dmv.ca.gov"}


@dataclass(frozen=True)
class Rule:
    id: str
    category: str
    strength: str
    reason: str
    pattern: re.Pattern
    also: Optional[re.Pattern] = None         # must ALSO match (AND)
    needs_url: bool = False
    kinds: frozenset = ALL
    on: str = "text"                           # which normalised field to match: text | leet | raw

    @property
    def weight(self) -> float:
        return STRENGTH[self.strength]


def R(pattern: str) -> re.Pattern:
    return re.compile(pattern, re.I)


CLAIM_CONTACT = R(r"\b(telegram|t\.me|whats ?app|wa\.me|signal|snap(chat)?|kik|dm me|message me|text me|contact me|inbox me|link in (my )?bio|check (my )?bio)\b")
MONEY = R(r"(\$\s?\d[\d,.]*\s?k?\b|\b\d[\d,.]*\s?(k|usd|usdt|dollars|btc|eth|bitcoin|coins|gifts?|grand|bands|racks)\b|\biphone\b|\bps5\b|gift\s?card)")

RULES: list[Rule] = [
    # ---- live chat and comments ----------------------------------------------------
    Rule("growth_service_ad", "live_spam", "near_certain", "Advertises bought followers or viewers",
         R(r"\b(wanna|want to)\s+(become|be)\s+famous\b|\bbuy\s+(followers|primes?|views|viewers)\b|\bin search of\s+followers\b|"
           r"\b(best|cheap|real)\s+(viewers|followers|views|chatters)\s+(on|at|from)\b|\b(streamboo|bigfollows|dogehype)\b"),
         kinds=SOCIAL),
    Rule("winner_contact", "prize_scam", "near_certain", "Tells you you've won and to contact them off the platform",
         R(r"\b(congrat\w*|you('| ha)?ve (been )?(selected|chosen|won)|lucky winners?|shortlisted)\b"), also=CLAIM_CONTACT),
    Rule("claim_prize_dm", "prize_scam", "strong", "Asks you to message them to claim a prize",
         R(r"\b(dm|message|text|contact|inbox)\s+me\b.{0,30}\b(claim|prize|reward|winnings|receive)\b")),
    Rule("first_n_prize", "prize_scam", "strong", "\"First N people / everyone who types X gets $\" prize bait",
         R(r"\b(first|next|top)\s+\d{1,4}\s+(people|viewers|users|ppl|persons|followers)\b|\b(1st|first|any|every|each)\s+(person|people|one|viewer)\s+(to|who)\b|\b(message|dm|text|inbox)\s+me\s+[\"'“]?\w{2,15}[\"'”]?\s*(to|and)?\s*(get|win|claim|receive)?\b.{0,0}(?=.*\b(paying|pay|giving|send|sending|get|win)\b)|\b(everyone|anyone)\s+who\s+(comments?|types?|follows?|dms?)\b|"
           r"\b(type|comment|say)\s+[\"'“]?\w{1,15}[\"'”]?\s+(to|and)\s+(get|win|claim|receive)\b"), also=MONEY),
    Rule("pay_to_message", "prize_scam", "strong", "Offers money to whoever messages them (the \"$7,000 to message me\" scam)",
         R(r"\b(message|dm|text|inbox|pm|hit)\s+me\b"),
         also=R(r"\b(paying|giving|giving away|sending|i'?ll\s+(pay|send|give)|i\s+(will|am)\s+(pay|send|giv)\w*)\b.{0,40}"
                r"(\$\s?\d|\b\d[\d,.]*\s?(k|grand|bands|usd|dollars)\b)|"
                r"(\$\s?\d[\d,.]*\s?k?|\b\d[\d,.]*\s?(k|grand|bands)\b)\s*(for|to)\s+(the\s+)?(any|every|each|first|1st|anyone|whoever)\b"),
         kinds=SOCIAL),
    Rule("fake_gift_claim", "prize_scam", "strong", "Claims to have sent you a gift or donation, then points you elsewhere",
         R(r"\b(i('| a)?m|i just|just)\s+(donat\w+|sent|gift\w*)\b.{0,80}\b(dm|bio|profile|follow me|telegram|whats ?app)\b")),
    Rule("crypto_doubling", "crypto_scam", "strong", "Promises to double or multiply crypto you send",
         R(r"\b(double|2x|x2|triple|multiply)\b.{0,40}\b(btc|bitcoin|eth|ethereum|usdt|sol|xrp|doge|crypto|coins?)\b|\bsend\b.{0,30}\b(back|return)\b.{0,20}\b(double|2x)\b|"
           r"\bsend\s+[\d.]+\s*(btc|bitcoin|eth|usdt|sol|xrp|doge)\b.{0,40}\b(get|receive)\s+[\d.]+\s*(btc|bitcoin|eth|usdt|sol|xrp|doge)\b")),
    Rule("wallet_address", "crypto_scam", "strong", "Posts a crypto wallet address",
         R(r"\b(bc1[ac-hj-np-z02-9]{11,71}|0x[a-f0-9]{40}|T[1-9A-HJ-NP-Za-km-z]{33})\b"), kinds=SOCIAL | PRIVATE, on="raw"),
    Rule("celebrity_giveaway", "crypto_scam", "medium", "Celebrity or exchange \"giveaway\" (a classic hijacked-stream scam)",
         R(r"\b(elon|musk|tesla|spacex|mr\.?\s?beast|saylor|ripple|binance|coinbase)\b.{0,40}\b(giveaway|airdrop|double|claim|qr|event)\b")),
    Rule("check_bio_funnel", "funnel", "medium", "Pushes you to their bio, Snapchat or DMs",
         R(r"\b(check|see|look at|visit)\s+(out\s+)?(my|the)\s+(bio|profile|page|link)\b|\blink in (my )?(bio|profile)\b|"
           r"\badd me on\s+(snap|sc|telegram|kik|insta|ig)\b|\b(sc|snap)\s*[:\-]\s*\w+|\bdm (me )?for\b"), kinds=SOCIAL),
    Rule("adult_bait", "funnel", "strong", "Adult bait pointing to their profile or another app",
         R(r"\b(lonely|horny|18\+|nudes?|sexy|hot pics|private content|onlyfans|0nlyf\w*|don'?t look at my (profile|pics?))\b"),
         also=R(r"\b(bio|profile|dm|snap|telegram|link|pics?|page)\b"), kinds=SOCIAL | PRIVATE, on="leet"),
    Rule("follow_for_follow", "engagement_bait", "weak", "Follow-for-follow / sub-for-sub spam",
         R(r"\b(f4f|follow\s*(4|for)\s*follow|sub\s*(4|for)\s*sub|l4l|like\s*(4|for)\s*like)\b"), kinds=SOCIAL),
    Rule("recovery_hacker", "recovery_scam", "near_certain", "Recommends a \"hacker\" or \"recovery expert\" (a recovery scam)",
         R(r"\b(hacker|recover(y|ed)?\s+(expert|specialist|agent)|account\s+recovery|recover(ed)?\s+my\s+(account|funds|crypto|money))\b"),
         also=R(r"(@\w{3,}|\b(contact|reach|dm|message|helped me|recommend|thanks to)\b)"), kinds=SOCIAL | PRIVATE),
    Rule("advisor_testimonial", "investment_scam", "strong", "Testimonial for a \"financial advisor\" or crypto mentor",
         R(r"\b(thanks to|working with|recommend|introduced (me )?to|helped me)\b.{0,60}\b(financial advis[eo]r|broker|mentor|trader|account manager|expert)\b|"
           r"\b(mrs?|ms|miss|dr)\.?\s+[a-z]+\b.{0,60}\b(profit|returns?|invest\w*|trading|portfolio)\b"), kinds=SOCIAL | PRIVATE),
    Rule("profit_testimonial", "investment_scam", "strong", "Brags about big trading or crypto profits (a pitch for a fake \"coach\" or platform)",
         R(r"\b(i|we)\s+(just\s+)?(made|earned|received|withdrew|profited|got paid)\s+(over\s+)?\$\s?\d[\d,.]*\s*k?\b.{0,80}"
           r"\b(trading|invest\w*|crypto|forex|bitcoin|btc|coach|mentor|platform|advis[eo]r|expert|account manager|signals?)\b"),
         kinds=SOCIAL | PRIVATE),
    Rule("text_art_flood", "flood", "strong", "Giant letters drawn with symbols (text art) that swamp the chat",
         R("(?:[\u2500-\u259F\u2800-\u28FF\u25A0-\u25FF][^\u2500-\u259F\u2800-\u28FF\u25A0-\u25FF]{0,8}){14,}"),
         kinds=SOCIAL, on="raw"),
    Rule("link_spam_template", "link_spam", "strong", "Link-bait template (\"finally it's here\", \"thank me later\", \"full video on my page\")",
         R(r"\b(finally it'?s here|it'?s finally here|i think you'?re looking for this|here is the (full|recommended) (video|clip)|"
           r"link to the clip|thank me later|full video is (up )?on my (page|profile|channel)|check out the full|here is the backup)\b"),
         needs_url=True, kinds=SOCIAL),
    Rule("bait_self_promo", "funnel", "strong", "Bait-style self-promotion (\"MY CONTENT IS...\", \"omg, exactly what I needed\")",
         R(r"\b(omg,? exactly what (i|you|people) needed|my (content|videos) (is|are) (better|way better)|i make way better content|read my profile|tap (on )?my (pic|picture|profile))\b"),
         kinds=SOCIAL),
    Rule("ai_refusal_leak", "ai_bot", "near_certain", "Leaked AI chatbot text (\"As an AI language model...\"): an automated account",
         R(r"\bas an ai( language)? model\b|\bas a large language model\b|\bi('m| am) (sorry|unable)[^.]{0,40}\b(cannot|can't|unable to) (generate|create|fulfill|comply|provide|assist)\b|"
           r"\bi cannot fulfill (that|this) request\b|\bmy (knowledge|training) cutoff\b|\bi (don't|do not) have access to (real-time|the video)\b"),
         kinds=SOCIAL | PRIVATE),
    Rule("ai_template_residue", "ai_bot", "strong", "Left-over template placeholders from an automated script",
         R(r"\{\{?\s*\w+\s*\}?\}|\[(name|username|product|video title|insert[^\]]*)\]|</?(comment|reply)>|^(comment|reply|response)\s*:"),
         kinds=SOCIAL | PRIVATE, on="raw"),
    # ---- email -------------------------------------------------------------------------
    Rule("callback_phishing", "email_scam", "strong", "Fake invoice or renewal asking you to call a number (callback phishing)",
         R(r"\b(geek\s*squad|norton|mcafee|paypal|best\s*buy|antivirus|subscription|order)\b.{0,200}\b(renew\w*|invoice|charged|payment|order)\b"),
         also=R(r"(\+?1?[\s.(-]*\d{3}[\s.)-]*\d{3}[\s.-]*\d{4}).{0,120}\b(call|contact|cancel|dispute|refund)\b|\b(call|contact|cancel|dispute|refund)\b.{0,120}(\+?1?[\s.(-]*\d{3}[\s.)-]*\d{3}[\s.-]*\d{4})"),
         kinds=frozenset({"email"})),
    Rule("email_sextortion", "sextortion", "near_certain", "Email sextortion (\"I recorded you\" + bitcoin demand). It's a bluff: don't pay",
         R(r"\b(recorded you|your (webcam|camera)|infected your (device|computer)|pervert|adult (site|videos?))\b"),
         also=R(r"\b(bitcoin|btc|wallet|bc1[a-z0-9]{20,}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b"), kinds=frozenset({"email"})),
    Rule("gift_card_bec", "email_scam", "strong", "Boss or friend urgently asking for gift cards",
         R(r"\b(buy|purchase|get)\b.{0,40}\b(gift\s*cards?|itunes|google\s*play|steam)\b.{0,120}\b(scratch|codes?|pictures?|urgent|asap|quick)\b"),
         kinds=frozenset({"email"}) | PRIVATE),
    # ---- private messages (DMs and texts) ---------------------------------------------
    Rule("wrong_number_opener", "wrong_number", "weak", "A \"wrong number\" opener, the usual start of a long con",
         R(r"^\W*(hi|hello|hey)?[,!\s]*(is\s+this|are\s+you)\s+[a-z]+\s*\??\s*$|\b(are\s+we\s+still\s+on|still\s+on\s+for|still\s+interested\s+in\s+touring)\b|"
           r"\b(golf|dinner|tee\s*time|lunch)\s+(tomorrow|tonight|this\s+weekend)\b|\bdid\s+you\s+send\s+those\s+documents\b"), kinds=PRIVATE),
    Rule("wrong_number_pivot", "wrong_number", "medium", "\"Sorry, wrong number... but nice to meet you\"",
         R(r"\b(sorry|apolog\w*)\b.{0,40}\b(wrong\s+number|disturb\w*)\b.{0,80}\b(fate|destiny|nice\s+to\s+meet|be\s+friends|where\s+are\s+you\s+from)\b"),
         kinds=PRIVATE),
    Rule("move_to_other_app", "wrong_number", "medium", "Asks to move the chat to WhatsApp or Telegram",
         R(r"\b(add\s+me\s+on|my\s+(whats\s?app|telegram)\s+(is|number)|let'?s\s+(talk|chat|continue)\s+on|move\s+to)\s+(whats\s?app|telegram|line|signal|wechat)?|\bwa\.me/|\bt\.me/|\+44\s?7\d{3}"),
         kinds=PRIVATE),
    Rule("investment_pitch", "investment_scam", "strong", "Crypto or \"investment platform\" pitch",
         R(r"\b(usdt|mining\s+pool|liquidity\s+(mining|pool)|trading\s+platform|contract\s+trading|gold\s+futures|guaranteed\s+(profit|returns?)|"
           r"my\s+(uncle|aunt)\s+(is|works)|insider\s+(tip|info)|forex\s+signals?|daily\s+profit)\b"), kinds=PRIVATE | frozenset({"comment"})),
    Rule("sextortion_threat", "sextortion", "near_certain", "Sextortion threat (if you're being threatened, stop replying and report it)",
         R(r"\bi\s+have\s+(your|ur)\s+(nudes?|pics?|photos?|video)\b|\bsend\s.{0,20}(to|all)\s+(your|ur)\s+(followers|friends|family|school)\b|"
           r"\b(ruin|destroy)\s+your\s+(life|reputation)\b|\bpay\s+(me|now).{0,40}(gift\s*card|cash\s*app|apple\s*pay|bitcoin|usdt)\b"), kinds=PRIVATE),
    Rule("is_this_you_video", "phishing", "strong", "\"Is this you in this video?\" link (steals your login)",
         R(r"\b(is\s+(this|that|it)\s+you|you\s+(appear|are)\s+in\s+this\s+video|look\s+what\s+i\s+found|i\s+found\s+(this|a\s+video)\s+of\s+you)\b"),
         needs_url=True, kinds=PRIVATE | frozenset({"comment"})),
    Rule("fake_ambassador", "fake_offer", "medium", "\"Become our brand ambassador\" offer that needs you to buy or pay shipping",
         R(r"\b(brand\s+ambassador|become\s+(an?|our)\s+ambassador|dm\s+to\s+collab|collab(oration)?\s+opportunity)\b"),
         also=R(r"\b(use\s+(our\s+)?code|(only|just)\s+pay\s+(for\s+)?shipping|exclusive\s+discount|discount\s+code)\b"), kinds=SOCIAL | PRIVATE),
    Rule("job_pay_offer", "job_scam", "strong", "Unsolicited easy-money job offer",
         R(r"\$\s?\d{2,4}\s*(-|to|~)?\s*\$?\d{0,4}\s*(/|per|a)\s*(day|hour|hr)\b"),
         also=R(r"\b(remote|part[-\s]?time|flexible|work\s+from\s+home|no\s+experience|online\s+job)\b"), kinds=PRIVATE),
    Rule("easy_money_pitch", "job_scam", "strong", "\"Make $500 a day from home\" pitch that sends you to Telegram, WhatsApp or DMs",
         R(r"\b(make|earn|making|earning)\s+(up\s+to\s+)?\$\s?\d[\d,.]*\s*k?\s*(/|per|a|every)\s*(day|week|hour|hr)\b"),
         also=R(r"\b(telegram|whats\s?app|dm\s+me|message\s+me|text\s+me|link\s+in\s+(my\s+)?bio|check\s+my\s+bio)\b"), kinds=SOCIAL),
    Rule("task_scam", "job_scam", "strong", "\"Get paid to like videos / rate products\" task scam",
         R(r"\b(like\s+(videos|posts)|rate\s+(products|hotels|apps)|app\s+optimi[sz]ation|boost\s+(products|data)|complete\s+\d+\s+tasks|commission\s+per\s+task|merchant\s+tasks?)\b"),
         kinds=PRIVATE | frozenset({"comment"})),
    Rule("deposit_to_withdraw", "job_scam", "near_certain", "Asks you to deposit money to unlock pay (a task-scam hallmark)",
         R(r"\b(recharge|top[-\s]?up|negative\s+balance|unlock\s+(withdrawal|combo|tasks)|prepay)\b"), kinds=PRIVATE),
    Rule("recruiter_off_platform", "job_scam", "strong", "\"Recruiter\" pushing you off LinkedIn or to install an interview tool",
         R(r"\b(contact\s+(me|our\s+hr)\s+on\s+(whats\s?app|telegram)|linkedin\s+messaging\s+is\s+inconvenient|company\s+policy\s+requires|interview\s+(tool|app|software))\b"),
         kinds=PRIVATE),
    Rule("fake_platform_support", "phishing", "near_certain", "Fake \"platform support\" threatening to delete your account",
         R(r"\b(copyright\s+(violation|infringement)|intellectual\s+property\s+(policy|violation)|(permanently\s+)?(deleted|disabled|suspended)\s+(in|within)\s+(24|48|12)\s*h(ours)?|appeal\s+(now|here|form))\b"),
         kinds=PRIVATE),
    # ---- texts (smishing) -------------------------------------------------------------
    Rule("toll_smish", "smishing", "near_certain", "Unpaid-toll text (toll agencies don't text demands like this)",
         R(r"\b(e-?z\s?pass|fastrak|sunpass|i-?pass|peach\s?pass|txtag|toll\s*(road|services?|by\s*plate)|unpaid\s+toll|outstanding\s+toll)\b"),
         also=R(r"\b(late\s+fee|penalt(y|ies)|dmv|suspend\w*|legal\s+action|within\s+\d+\s*h(ours)?|pay\s+now)\b"), needs_url=True, kinds=PRIVATE | frozenset({"email"})),
    Rule("toll_amount", "smishing", "strong", "The exact toll-scam amounts ($12.51 balance, $50 late fee)",
         R(r"\$\s?\d{1,2}\.\d{2}.{0,60}\$\s?50\b"), kinds=PRIVATE),
    Rule("package_smish", "smishing", "strong", "Fake delivery problem asking you to click a link",
         R(r"\b(usps|ups|fedex|dhl|parcel|package|shipment)\b.{0,120}\b(incomplete\s+address|unable\s+to\s+deliver|cannot\s+be\s+delivered|redeliver(y)?|held\s+at\s+(the\s+)?warehouse|update\s+(your\s+)?address|within\s+(12|24|48)\s*h)"),
         needs_url=True, kinds=PRIVATE | frozenset({"email"})),
    Rule("account_lock_smish", "smishing", "strong", "Fake account-locked alert with a link",
         R(r"\b(apple\s*id|icloud|amazon|paypal|netflix|bank)\b.{0,80}\b(locked|suspended|disabled|verify|unusual\s+sign[-\s]?in)\b"),
         needs_url=True, kinds=PRIVATE),
    Rule("tax_smish", "smishing", "strong", "Fake tax refund or stimulus text (the IRS doesn't text first)",
         R(r"\b(irs|tax\s+refund|stimulus|rebate)\b.{0,80}\b(claim|eligible|pending|verify)\b"), needs_url=True, kinds=PRIVATE),
    Rule("code_theft", "account_takeover", "near_certain", "Asks you to send them a verification code (never share codes)",
         R(r"\b(sent|went)\s+(you\s+)?(a|the|my)?\s*(code|otp|verification)\b.{0,40}\b(by\s+mistake|accident(ally)?|wrong\s+number)\b|"
           r"\b(code|otp|verification)\b.{0,30}\b(went|sent|came)\s+to\s+(you|your\s+(phone|number))\b.{0,40}\b(mistake|accident\w*|wrong)\b|"
           r"\b(send|forward|share|read)\s+(me\s+)?(the|that|your)\s+(\d-digit\s+)?(code|otp)\b"), kinds=PRIVATE),
    Rule("reply_y_trick", "smishing", "near_certain", "\"Reply Y and reopen\" trick to switch your phone's link protection back on",
         R(r"\breply\s+[\"']?y(es)?[\"']?\b.{0,80}\b(exit|re-?open|copy\s+the\s+link|activate\s+the\s+link|safari)\b"), kinds=PRIVATE),
    Rule("prize_text", "smishing", "strong", "\"You've won\" text with a link",
         R(r"\b(congratulations|you('ve|\s+have)\s+(won|been\s+selected)|claim\s+your\s+(prize|reward|gift\s*card)|free\s+(iphone|gift))\b"),
         needs_url=True, kinds=PRIVATE),
]

# ---------------------------------------------------------------------------------
# Link checks


def link_findings(urls: Iterable[str]) -> list[tuple[str, str, str]]:
    """(code, strength, reason) for suspicious links."""
    out = []
    for u in urls:
        u = u.strip().rstrip(").,!?")
        host = (urlparse(u if "://" in u else "http://" + u).hostname or "").lower()
        if not host:
            continue
        labels = host.split(".")
        registered = ".".join(labels[-2:])
        tld = labels[-1]
        if re.match(r"^com-|-com$", labels[-2] if len(labels) >= 2 else ""):
            out.append(("url_com_dash", "near_certain", f"Link uses a fake \"com-\" domain ({host})"))
        if registered not in REAL_BRAND_DOMAINS and any(b in host for b in BRAND_WORDS):
            out.append(("url_brand_lookalike", "strong", f"Link puts a brand name on someone else's site ({host})"))
        if tld in CHEAP_TLDS:
            out.append(("url_cheap_tld", "weak", f"Link on a throwaway-style domain ending .{tld}"))
        if registered in SHORTENERS:
            out.append(("url_shortener", "weak", "Hides where the link goes with a shortener"))
        if re.fullmatch(r"[\d.]+", host):
            out.append(("url_raw_ip", "strong", "Link to a bare IP address"))
        if "xn--" in host:
            out.append(("url_punycode", "strong", f"Link uses lookalike international characters ({host})"))
        if re.search(r"(workers\.dev|pages\.dev|r2\.dev|firebaseapp\.com|web\.app|ipfs\.|\.ipfs\.|glitch\.me|000webhostapp\.com|weebly\.com|wixsite\.com)$", host) \
                or "/ipfs/" in u:
            out.append(("url_free_hosting", "medium", f"Link is on free hosting that phishers often abuse ({host})"))
    return out


# ---------------------------------------------------------------------------------
# Evaluation


@dataclass
class Hit:
    code: str
    strength: str
    reason: str
    category: str = ""

    @property
    def weight(self) -> float:
        return STRENGTH[self.strength]


# People talking ABOUT spam ("anyone else getting the streamboo bot?", "someone commented
# 'as an AI language model' lol") quote the very phrases the rules look for.
DISCUSSING = R(r"\b(anyone else (get|getting|seeing|see)|someone (commented|sent|posted|wrote|dm'?d)|these bots|the bots|bots? (are|is) everywhere|"
               r"spam(mers| bots?)?\b|scam(mers)?\b|got (this|a|the) (weird )?(text|message|dm|comment)|is this a scam|beware|watch out|reported (it|them)|lol .* bot)")
QUOTED = re.compile(r"[\"'“‘][^\"'”’]{6,}[\"'”’]")


def evaluate(text: str, kind: str) -> list[Hit]:
    c = normalize(text)
    hits: list[Hit] = []
    urls = URL.findall(c.raw) + (URL.findall(c.text) if c.obfuscated_link else [])
    has_url = bool(urls) or c.obfuscated_link or bool(_TLD_URL.search(c.text))
    discussing = bool(DISCUSSING.search(c.text)) and not has_url and not CLAIM_CONTACT.search(c.text)
    for r in RULES:
        if kind not in r.kinds:
            continue
        subject = {"text": c.text, "leet": c.leet, "raw": c.raw}[r.on]
        if not r.pattern.search(subject):
            continue
        if r.also is not None and not (r.also.search(c.text) or r.also.search(c.leet)):
            continue
        if r.needs_url and not has_url:
            continue
        if discussing or (QUOTED.search(c.raw) and not has_url and r.category in ("ai_bot", "live_spam")):
            hits.append(Hit(r.id, "weak", r.reason + " (quoted or discussed, so only a hint)", r.category))
            continue
        hits.append(Hit(r.id, r.strength, r.reason, r.category))
    if c.obfuscated_link:
        hits.append(Hit("obfuscated_link", "strong", "Disguises a link to slip past filters (like \"site . com\")", "evasion"))
    if c.mixed_script:
        hits.append(Hit("mixed_script", "strong", "Mixes lookalike letters from other alphabets to dodge filters", "evasion"))
    if c.zalgo:
        hits.append(Hit("zalgo_text", "medium", "Stacks accents on letters to dodge filters", "evasion"))
    for code, strength, reason in link_findings(urls):
        hits.append(Hit(code, strength, reason, "link"))
    return hits


# Conversation-level pattern: wrong-number opener -> move to another app -> money pitch.
SEQUENCE_STEPS = ({"wrong_number_opener", "wrong_number_pivot"}, {"move_to_other_app"}, {"investment_pitch", "crypto_doubling", "wallet_address"})


def sequence_hit(ordered_codes: list[set[str]]) -> Optional[Hit]:
    """Given each message's rule codes in time order, detect the pig-butchering arc."""
    step = 0
    for codes in ordered_codes:
        if step < len(SEQUENCE_STEPS) and codes & SEQUENCE_STEPS[step]:
            step += 1
        elif step >= 1 and codes & SEQUENCE_STEPS[-1]:
            step = len(SEQUENCE_STEPS)
    if step == len(SEQUENCE_STEPS):
        return Hit("pig_butchering_arc", "near_certain",
                   "Followed the \"wrong number\" to new app to investment pitch pattern of a pig-butchering scam", "investment_scam")
    return None


def weights() -> dict[str, float]:
    w = {r.id: r.weight for r in RULES}
    w.update({"obfuscated_link": STRENGTH["strong"], "mixed_script": STRENGTH["strong"], "zalgo_text": STRENGTH["medium"],
              "url_com_dash": STRENGTH["near_certain"], "url_brand_lookalike": STRENGTH["strong"], "url_cheap_tld": STRENGTH["weak"],
              "url_shortener": STRENGTH["weak"], "url_raw_ip": STRENGTH["strong"], "pig_butchering_arc": STRENGTH["near_certain"],
              "url_punycode": STRENGTH["strong"], "url_free_hosting": STRENGTH["medium"], "bait_username": STRENGTH["strong"],
              "pinned_by_impersonation": STRENGTH["near_certain"], "stolen_comment": STRENGTH["strong"],
              "dangerous_attachment": STRENGTH["strong"], "html_smuggling": STRENGTH["near_certain"]})
    return w



# ---------------------------------------------------------------------------------
# Usernames (bots put their pitch in the name so it shows on every comment)

BAIT_NAME = R(r"(on telegram|via telegram|telegram me|tlgrm|on nicegram|on instagram|on ig\b|via ig\b|whats ?app me|text me on|via gmail|a gmail com|"
              r"check my cha|see my cha|visit my cha|go to my cha|check out my|on my profile|tap me|sub 4 sub|subs to me|im subbing|"
              r"dont (read|look( at)?) my|free gift|everyone who|with (o|no) vid|without any vid|subs challenge|^dm me\b|\bdm me (for|with|now|asap)\b|message me for)")
PINNED = R(r"pinned\s*by")


_VOWELS = set("aeiou")
_REAL_NO_VOWELS = {"rhythm", "rhythms", "crypt", "crypts", "lynx", "myths", "glyph", "glyphs", "nymph", "nymphs", "psych", "synth",
                   "synths", "gypsy", "tryst", "flyby", "shyly", "slyly", "dryly", "lymph", "pygmy", "sylph", "xysts"}


def keyboard_mash(name: str) -> bool:
    """Names made of random letters ("yznnkdcp", "ynvpxs", "cbeanjhgccf"): no real name reads like that."""
    for part in re.split(r"[\s._\-]+", (name or "").lower().lstrip("@")):
        letters = re.sub(r"[^a-z]", "", part)
        if len(letters) < 5 or len(letters) != len(part):
            continue
        vowels = sum(ch in _VOWELS for ch in letters)
        if vowels == 0 and letters not in _REAL_NO_VOWELS:         # ynvpxs
            return True
        if len(letters) >= 8 and re.search(r"[bcdfghjklmnpqrstvwxz]{5,}", letters):   # yznnkdcp, cbeanjhgccf
            return True
    return False


def evaluate_username(name: str) -> list[Hit]:
    c = normalize(name or "")
    squashed = re.sub(r"[\s_.\-*|]+", " ", c.leet)
    hits = []
    if PINNED.search(squashed) or PINNED.search(squashed.replace(" ", "")):
        hits.append(Hit("pinned_by_impersonation", "near_certain", "Name pretends to be a \"Pinned by\" creator badge", "impersonation"))
    if BAIT_NAME.search(squashed):
        hits.append(Hit("bait_username", "strong", "The account name itself is an ad (\"on telegram\", \"check my channel\")", "funnel"))
    return hits


# ---------------------------------------------------------------------------------
# Email attachments

DANGEROUS_EXT = re.compile(r"\.(exe|scr|js|jse|vbs|vbe|wsf|hta|iso|img|lnk|bat|cmd|ps1|msi|one|html?|svg)$", re.I)
DOUBLE_EXT = re.compile(r"\.(pdf|docx?|xlsx?|jpg|png|txt)\.(exe|scr|js|vbs|html?|lnk|bat|cmd)$", re.I)


def attachment_findings(filenames: list[str], html: str = "") -> list[Hit]:
    hits = []
    for f in filenames:
        if DOUBLE_EXT.search(f):
            hits.append(Hit("dangerous_attachment", "strong", f"Attachment hides its real type ({f})", "email"))
            break
        if DANGEROUS_EXT.search(f):
            hits.append(Hit("dangerous_attachment", "strong", f"Risky attachment type ({f})", "email"))
            break
    if html and re.search(r"atob\(", html) and re.search(r"(new\s+Blob\(|msSaveOrOpenBlob|URL\.createObjectURL)", html):
        hits.append(Hit("html_smuggling", "near_certain", "Hidden code that builds a download inside the email (HTML smuggling)", "email"))
    return hits
