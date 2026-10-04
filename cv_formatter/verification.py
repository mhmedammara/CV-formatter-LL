"""Contrôle déterministe anti-invention.

Chaque information extraite est confrontée au texte du CV d'origine :
- tout nombre, date, sigle, nom propre ou outil doit figurer dans le CV ;
- e-mail, téléphone et LinkedIn doivent y être visibles ;
- pour un CV en français, une formulation trop éloignée du texte d'origine est signalée.
Un élément introuvable est retiré (ou seulement signalé si le texte de référence est
incertain : OCR, JSON corrigé à la main).
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field

from .schema import CV

STOPWORDS = set(
    """
    dans avec pour sans sous chez entre vers depuis pendant selon leurs leur ainsi plus moins mais tout tous toute
    toutes cette ceux celle celles elles etre avoir sont etait ont fait faire afin lors dont comme aussi tres bien
    autre autres chaque quelques notamment travers aupres auprès dela deja the and with from that this into over
    their have were been your about other such than then when where which while within without using used based
    pour une des les aux sur par que qui son ses nos vos est ete
    """.split()
)

NUMBER_WORDS = {
    "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5, "six": 6, "sept": 7, "huit": 8, "neuf": 9,
    "dix": 10, "onze": 11, "douze": 12, "treize": 13, "quatorze": 14, "quinze": 15, "seize": 16, "vingt": 20,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
}


def norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = unicodedata.normalize("NFD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return text.lower().replace("’", "'").replace("œ", "oe").replace("æ", "ae")


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", norm(text))


@dataclass
class Reference:
    """Index du texte du CV d'origine."""

    tokens: set[str]
    vocab: dict[int, list[str]]
    compact: str
    digits: str
    flat: str
    links: str
    number_words: set[int]

    @classmethod
    def build(cls, text: str, links: list[str]) -> "Reference":
        # On découpe aussi aux frontières lettres/chiffres : « de140 » (texte collé) donne « de » et « 140 ».
        toks = tokens(text) + re.findall(r"[a-z]+|[0-9]+", norm(text))
        vocab: dict[int, list[str]] = {}
        for tok in set(toks):
            if len(tok) >= 4:
                vocab.setdefault(len(tok), []).append(tok)
        numbers = {NUMBER_WORDS[t] for t in toks if t in NUMBER_WORDS}
        if re.search(r"dix[\s-]sept", norm(text)):
            numbers.add(17)
        return cls(
            tokens=set(toks),
            vocab=vocab,
            compact=re.sub(r"[^a-z0-9]", "", norm(text)),
            digits=re.sub(r"\D", "", text),
            flat=re.sub(r"\s+", " ", norm(text)),
            links=norm(" ".join(links)),
            number_words=numbers,
        )

    def has(self, tok: str) -> bool:
        if tok in self.tokens:
            return True
        if tok.isdigit():
            return (len(tok) >= 4 and tok in self.compact) or int(tok) in self.number_words
        if len(tok) >= 4 and tok in self.compact:
            return True
        if len(tok) >= 5:
            pool = [w for n in range(len(tok) - 2, len(tok) + 3) for w in self.vocab.get(n, [])]
            return bool(difflib.get_close_matches(tok, pool, n=1, cutoff=0.85))
        return False


@dataclass
class Finding:
    path: tuple
    label: str
    text: str
    reason: str
    action: str  # "retiré" | "à vérifier"


@dataclass
class TextCheck:
    missing: list[str] = field(default_factory=list)  # éléments introuvables (bloquants)
    overlap: float | None = None


_WORD = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ0-9][\wÀ-ÖØ-öø-ÿ'’.+#&/-]*")


def check_text(text: str, ref: Reference, french_source: bool) -> TextCheck:
    result = TextCheck()
    if not text or not text.strip():
        return result
    for match in _WORD.finditer(text):
        word = match.group(0).strip(".'’-/")
        if not word:
            continue
        before = text[: match.start()].rstrip()
        sentence_start = not before or before[-1] in ".:;!?(«\"“–—•-|"
        has_digit = any(c.isdigit() for c in word)
        letters = [c for c in word if c.isalpha()]
        acronym = len(letters) >= 2 and all(c.isupper() for c in letters)
        camel = any(c.isupper() for c in word[1:]) and not acronym
        proper = word[:1].isupper() and not sentence_start and french_source
        if not (has_digit or acronym or camel or proper):
            continue
        for tok in tokens(word):
            # « 3ème » / « 3ième », « v8 » : on contrôle séparément la partie chiffrée et la partie lettres.
            parts = re.findall(r"[a-z]+|[0-9]+", tok) if any(c.isdigit() for c in tok) and not tok.isdigit() else [tok]
            if ref.has(tok):
                continue
            for part in parts:
                if part.isalpha() and len(part) <= 4 and len(parts) > 1:
                    continue  # suffixe court (ème, x, v…) : seule la partie chiffrée compte
                if not ref.has(part) and tok not in result.missing:
                    result.missing.append(tok)
    if french_source:
        content = [t for t in tokens(text) if len(t) >= 4 and t not in STOPWORDS and not t.isdigit()]
        if len(content) >= 3:
            result.overlap = sum(ref.has(t) for t in content) / len(content)
    return result


class Verifier:
    def __init__(self, cv: CV, reference_text: str, links: list[str], apply_removals: bool):
        self.cv = cv
        self.ref = Reference.build(reference_text, links)
        self.french = (cv.langue_source or "fr").lower().startswith("fr")
        self.apply = apply_removals
        self.findings: list[Finding] = []

    # -- utilitaires --
    def _flag(self, path: tuple, label: str, text: str, reason: str, removable: bool = True) -> bool:
        """Enregistre un constat ; renvoie True si l'élément doit être retiré."""
        remove = removable and self.apply
        self.findings.append(Finding(path, label, text, reason, "retiré" if remove else "à vérifier"))
        return remove

    def _check(self, path: tuple, label: str, text: str | None, removable: bool = True) -> bool:
        """Contrôle un texte ; renvoie True s'il faut le retirer."""
        if not text:
            return False
        result = check_text(text, self.ref, self.french)
        if result.missing:
            return self._flag(path, label, text, "introuvable dans le CV : " + ", ".join(result.missing), removable)
        if result.overlap is not None and result.overlap < 0.6:
            self._flag(path, label, text, f"formulation éloignée du CV ({int(result.overlap * 100)} % des mots retrouvés)", False)
        return False

    def _filter_list(self, items: list[str], path: tuple, label: str) -> list[str]:
        kept = []
        for i, item in enumerate(items):
            if not self._check(path + (i,), f"{label} : {item}", item):
                kept.append(item)
        return kept

    # -- contrôles par rubrique --
    def _contact(self) -> None:
        c = self.cv.contact
        if c.email and norm(c.email.strip()) not in self.ref.flat:
            if self._flag(("contact", "email"), "E-mail", c.email, "absent du texte visible du CV"):
                c.email = None
        if c.telephone:
            digits = re.sub(r"\D", "", c.telephone)
            if len(digits) < 8 or digits[-9:] not in self.ref.digits:
                if self._flag(("contact", "telephone"), "Téléphone", c.telephone, "numéro introuvable dans le CV"):
                    c.telephone = None
        if c.linkedin:
            handle = re.split(r"/in/", c.linkedin.rstrip("/"))[-1]
            key = re.sub(r"[^a-z0-9]", "", norm(handle))
            if not key or (key not in self.ref.compact and key not in re.sub(r"[^a-z0-9]", "", self.ref.links)):
                if self._flag(("contact", "linkedin"), "LinkedIn", c.linkedin, "introuvable dans le CV ni dans ses liens"):
                    c.linkedin = None
        if c.localisation and self._check(("contact", "localisation"), "Localisation", c.localisation):
            c.localisation = None

    def _annees(self) -> None:
        a = self.cv.annees_experience
        if a.valeur is None:
            return
        citation_ok = bool(a.citation) and (
            re.sub(r"\s+", " ", norm(a.citation)).strip() in self.ref.flat
            or (lambda t: bool(t) and sum(self.ref.has(x) for x in t) / len(t) >= 0.85)(tokens(a.citation))
        )
        value_in_citation = bool(a.citation) and (
            str(a.valeur) in tokens(a.citation)
            or any(NUMBER_WORDS.get(t) == a.valeur for t in tokens(a.citation))
        )
        if not (citation_ok and value_in_citation):
            if self._flag(("annees_experience",), "Années d'expérience", f"{a.valeur} (« {a.citation} »)", "mention introuvable dans le CV"):
                a.valeur, a.citation = None, None

    def _experiences(self) -> None:
        kept = []
        for i, exp in enumerate(self.cv.experiences):
            name = " — ".join(x for x in (exp.poste, exp.entreprise) if x) or f"expérience {i + 1}"
            base = ("experiences", i)
            for attr, label in (("poste", "Poste"), ("entreprise", "Entreprise"), ("client", "Client"), ("lieu", "Lieu"), ("contexte", "Contexte")):
                value = getattr(exp, attr)
                if self._check(base + (attr,), f"{name} — {label}", value):
                    setattr(exp, attr, None)
            if exp.periode_texte:
                if self._check(base + ("periode_texte",), f"{name} — Période", exp.periode_texte):
                    exp.periode_texte, exp.debut, exp.fin = None, None, None
            years = set(re.findall(r"(?:19|20)\d{2}", exp.periode_texte or ""))
            for attr in ("debut", "fin"):
                value = getattr(exp, attr)
                if value and value != "present" and value[:4] not in years:
                    self._flag(base + (attr,), f"{name} — Date", value, "date absente de la période écrite", False)
                    setattr(exp, attr, None)
            exp.realisations = self._filter_list(exp.realisations, base + ("realisations",), f"{name} — Réalisation")
            exp.environnement = self._filter_list(exp.environnement, base + ("environnement",), f"{name} — Environnement")
            if exp.poste or exp.entreprise or exp.client or exp.realisations:
                kept.append(exp)
        self.cv.experiences = kept

    def _projets(self) -> None:
        kept = []
        for i, prj in enumerate(self.cv.projets):
            base = ("projets", i)
            if self._check(base + ("nom",), f"Projet : {prj.nom}", prj.nom):
                continue
            if self._check(base + ("cadre",), f"Projet {prj.nom} — Cadre", prj.cadre):
                prj.cadre = None
            if self._check(base + ("periode_texte",), f"Projet {prj.nom} — Période", prj.periode_texte):
                prj.periode_texte = None
            prj.description = self._filter_list(prj.description, base + ("description",), f"Projet {prj.nom}")
            prj.environnement = self._filter_list(prj.environnement, base + ("environnement",), f"Projet {prj.nom} — Environnement")
            kept.append(prj)
        self.cv.projets = kept

    def _objects(self) -> None:
        certifs = []
        for i, cert in enumerate(self.cv.certifications):
            base = ("certifications", i)
            if self._check(base + ("intitule",), f"Certification : {cert.intitule}", cert.intitule):
                continue
            for attr in ("organisme", "date"):
                if self._check(base + (attr,), f"Certification {cert.intitule} — {attr}", getattr(cert, attr)):
                    setattr(cert, attr, None)
            certifs.append(cert)
        self.cv.certifications = certifs

        formations = []
        for i, form in enumerate(self.cv.formations):
            base = ("formations", i)
            if self._check(base + ("diplome",), f"Formation : {form.diplome}", form.diplome):
                continue
            for attr in ("etablissement", "lieu", "periode", "details"):
                if self._check(base + (attr,), f"Formation {form.diplome} — {attr}", getattr(form, attr)):
                    setattr(form, attr, None)
            formations.append(form)
        self.cv.formations = formations

        langues = []
        for i, lang in enumerate(self.cv.langues):
            base = ("langues", i)
            if self.french and self._check(base + ("langue",), f"Langue : {lang.langue}", lang.langue):
                continue
            if self._check(base + ("niveau",), f"Langue {lang.langue} — niveau", lang.niveau):
                lang.niveau = None
            langues.append(lang)
        self.cv.langues = langues

        cats = []
        for i, cat in enumerate(self.cv.competences_detaillees):
            base = ("competences_detaillees", i)
            cat.elements = self._filter_list(cat.elements, base + ("elements",), f"Compétences « {cat.categorie} »")
            if cat.elements:
                cats.append(cat)
        self.cv.competences_detaillees = cats

    def run(self) -> list[Finding]:
        cv = self.cv
        for attr, label in (("prenom", "Prénom"), ("nom", "Nom")):
            self._check((attr,), label, getattr(cv, attr), removable=False)
        if self._check(("titre",), "Titre", cv.titre):
            cv.titre = None
        self._contact()
        self._annees()
        cv.expertise = self._filter_list(cv.expertise, ("expertise",), "Expertise")
        cv.methodes = self._filter_list(cv.methodes, ("methodes",), "Méthode")
        cv.outils_si = self._filter_list(cv.outils_si, ("outils_si",), "Outil SI")
        self._objects()
        self._experiences()
        self._projets()
        return self.findings


def verify(cv: CV, reference_text: str, links: list[str], apply_removals: bool) -> tuple[CV, list[Finding]]:
    """Renvoie une copie du CV nettoyée et la liste des constats."""
    checked = cv.model_copy(deep=True)
    findings = Verifier(checked, reference_text, links, apply_removals).run()
    return checked, findings
