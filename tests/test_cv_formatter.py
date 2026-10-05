"""Tests de non-régression (données fictives uniquement : aucun vrai CV dans le dépôt).

Lancer : python -m pytest -q
"""

from __future__ import annotations

import io
from types import SimpleNamespace as NS

import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation

from cv_formatter import contenu
from cv_formatter.__main__ import photo_decision, safe_name
from cv_formatter.anonymisation import anonymize, initials
from cv_formatter.config import FONTS_DIR, TEMPLATE_PATH
from cv_formatter.controle import apply_verdicts, build_claims, photo_verdict
from cv_formatter.layout import Para, Run, line_count, para_height
from cv_formatter.pdf_source import fix_text, load_source
from cv_formatter.render_pptx import _split_long_para, render_cv
from cv_formatter.schema import CV, Verdict
from cv_formatter.verification import verify
from cv_formatter.visage import FaceCheck, check_face

SOURCE = """Jean DUPONT
Chef de projet SI
jean.dupont@example.com | 06 12 34 56 78 | Lyon (69003)
linkedin.com/in/jean-dupont
Expériences
Chef de projet SI — ACME Conseil (mission chez Banque Exemple) — Janvier 2020 – Présent
Pilotage d'un portefeuille de 200 projets IT avec Power BI et SAP S4/HANA
Animation des comités de pilotage et suivi du budget de 2,3 M€
Développeur — ACME Conseil — 2016 - 2019 (alternance)
Développement de l'outil de suivi de140 magasins
Formation
Master Management de projet — Université Exemple — 2014 - 2016
Certification Scrum PSPO I
Langues : Anglais courant (TOEIC 890), depuis trois ans en gestion de projet
INWI (3ième opérateur telecom)
"""


def make_cv(**overrides) -> CV:
    data = {
        "langue_source": "fr",
        "prenom": "Jean",
        "nom": "DUPONT",
        "titre": "Chef de projet SI",
        "contact": {"email": "jean.dupont@example.com", "telephone": "06 12 34 56 78", "localisation": "Lyon (69003)", "linkedin": "linkedin.com/in/jean-dupont"},
        "annees_experience": {"valeur": None, "citation": None},
        "expertise": ["Pilotage de portefeuille", "Gestion de projet"],
        "methodes": ["Scrum"],
        "outils_si": ["Power BI", "SAP S4/HANA"],
        "competences_detaillees": [],
        "langues": [{"langue": "Anglais", "niveau": "Courant (TOEIC 890)"}],
        "certifications": [{"intitule": "Scrum PSPO I", "organisme": None, "date": None}],
        "formations": [{"diplome": "Master Management de projet", "etablissement": "Université Exemple", "lieu": None, "periode": "2014 - 2016", "details": None}],
        "experiences": [
            {"poste": "Chef de projet SI", "entreprise": "ACME Conseil", "client": "Banque Exemple", "lieu": None,
             "periode_texte": "Janvier 2020 – Présent", "debut": "2020-01", "fin": "present", "type": "mission", "contexte": None,
             "realisations": ["Pilotage d'un portefeuille de 200 projets IT avec Power BI et SAP S4/HANA",
                              "Animation des comités de pilotage et suivi du budget de 2,3 M€"], "environnement": ["Power BI", "SAP S4/HANA"]},
            {"poste": "Développeur", "entreprise": "ACME Conseil", "client": None, "lieu": None, "periode_texte": "2016 - 2019",
             "debut": "2016", "fin": "2019", "type": "alternance", "contexte": None,
             "realisations": ["Développement de l'outil de suivi de 140 magasins"], "environnement": []},
        ],
        "projets": [],
        "photo_candidate": None,
        "sections_non_reprises": [],
    }
    data.update(overrides)
    return CV.model_validate(data)


# --- Contrôle déterministe -----------------------------------------------------------------


def test_faithful_extraction_is_untouched():
    checked, findings = verify(make_cv(), SOURCE, [], apply_removals=True)
    assert [f for f in findings if f.action == "retiré"] == []
    assert len(checked.experiences[0].realisations) == 2


@pytest.mark.parametrize(
    "bullet",
    [
        "Pilotage d'un portefeuille de 350 projets IT",  # nombre inventé
        "Certification PMP obtenue",  # sigle inventé
        "Migration vers Salesforce",  # outil inventé (nom propre)
    ],
)
def test_invented_bullet_is_removed(bullet):
    cv = make_cv()
    cv.experiences[0].realisations.append(bullet)
    checked, findings = verify(cv, SOURCE, [], apply_removals=True)
    assert bullet not in checked.experiences[0].realisations
    assert any(f.action == "retiré" and f.text == bullet for f in findings)


def test_tolerances_keep_true_information():
    cv = make_cv(outils_si=["PowerBI", "SAP S4/HANA"])  # forme collée
    cv.experiences[1].realisations = ["Développement de l'outil de suivi de 140 magasins"]  # « de140 » dans le PDF
    cv.experiences[0].entreprise = "INWI (3ème opérateur télécom)"  # « 3ième » dans le PDF
    _, findings = verify(cv, SOURCE + "\nACME Conseil", [], apply_removals=True)
    assert [f for f in findings if f.action == "retiré"] == []


def test_contact_must_be_visible():
    cv = make_cv(contact={"email": "autre.personne@example.com", "telephone": "07 99 99 99 99", "localisation": None, "linkedin": None})
    checked, _ = verify(cv, SOURCE, [], apply_removals=True)
    assert checked.contact.email is None and checked.contact.telephone is None


def test_doubtful_reference_only_flags():
    cv = make_cv()
    cv.experiences[0].realisations.append("Pilotage d'un portefeuille de 350 projets IT")
    checked, findings = verify(cv, SOURCE, [], apply_removals=False)
    assert len(checked.experiences[0].realisations) == 3
    assert all(f.action == "à vérifier" for f in findings)


# --- Contre-vérification -------------------------------------------------------------------


def test_cross_check_removes_atomic_items_but_never_whole_experiences():
    cv = make_cv()
    claims = {c.id: c for c in build_claims(cv)}
    verdicts = [Verdict(id=i, statut="confirme", explication="") for i in claims]
    for v in verdicts:
        if v.id in ("E0", "E0.R1", "C0"):
            v.statut = "absent"
    findings = apply_verdicts(cv, verdicts, apply_removals=True)
    assert len(cv.experiences) == 2  # E0 absent : signalé seulement
    assert len(cv.experiences[0].realisations) == 1  # E0.R1 absent : retiré
    assert cv.certifications == []  # C0 absent : retiré
    assert {f.action for f in findings} == {"à vérifier", "retiré"}


def test_cross_check_accepts_bracketed_ids():
    cv = make_cv()
    verdicts = [Verdict(id="[C0]", statut="absent", explication="introuvable")]
    apply_verdicts(cv, verdicts, apply_removals=True)
    assert cv.certifications == []


def test_nature_contradiction_excludes_from_years_without_removal():
    cv = make_cv()
    cv.experiences[0].type = "emploi"
    findings = apply_verdicts(cv, [Verdict(id="E0.T", statut="inexact", explication="stage")], apply_removals=True)
    assert cv.experiences[0].type == "autre" and len(cv.experiences) == 2
    assert findings and findings[0].action == "à vérifier"


def test_photo_verdict_parsing():
    assert photo_verdict([Verdict(id="[PHOTO]", statut="inexact", explication="badge")]) == (False, "badge")
    assert photo_verdict([Verdict(id="PHOTO", statut="confirme", explication="")]) == (True, "")
    assert photo_verdict([]) == (None, "")


# --- Années d'expérience ----------------------------------------------------------------------


def test_years_exclude_alternance_and_round_up():
    label, origin, _ = contenu.experience_label(make_cv())
    assert "alternance" in origin
    months = contenu.computed_years(make_cv())[1]
    assert "prouvés" in months
    # mission depuis janvier 2020 : arrondi supérieur
    import datetime as dt

    today = dt.date.today()
    expected = -(-((today.year - 2020) * 12 + today.month) // 12)
    assert label == f"{expected} ans d’expérience"


def test_no_experience_means_no_pill():
    cv = make_cv()
    cv.experiences = [cv.experiences[1]]  # seulement l'alternance
    label, _, _ = contenu.experience_label(cv)
    assert label is None


def test_internship_and_keywords_excluded():
    cv = make_cv()
    cv.experiences[0].type = "emploi"
    cv.experiences[0].poste = "Stage chef de projet"
    assert contenu.computed_years(cv)[0] is None or "stage" in contenu.computed_years(cv)[1]


def test_stated_years_never_exceeded_and_lowered_when_unproven():
    cv = make_cv(annees_experience={"valeur": 2, "citation": "2 ans"})
    assert contenu.experience_label(cv)[0] == "2 ans d’expérience"  # jamais plus que l'annonce
    cv = make_cv(annees_experience={"valeur": 30, "citation": "30 ans"})
    label, _, warning = contenu.experience_label(cv)
    assert label != "30 ans d’expérience" and "ramenées" in warning


def test_founder_role_counts():
    cv = make_cv()
    cv.experiences = [cv.experiences[0].model_copy(update={"type": "creation_entreprise", "debut": "2024-02", "periode_texte": "Depuis février 2024"})]
    assert contenu.computed_years(cv)[0] >= 1


# --- Photo ----------------------------------------------------------------------------------


def _badge() -> Image.Image:
    img = Image.new("RGB", (400, 400), "white")
    draw = ImageDraw.Draw(img)
    draw.ellipse((10, 10, 390, 390), fill=(255, 93, 36))
    font = ImageFont.truetype(str(FONTS_DIR / "Lexend-SemiBold.ttf"), 46)
    draw.text((200, 200), "CERTIFIED", fill="white", font=font, anchor="mm")
    return img


def test_badge_is_not_a_face():
    assert check_face(_badge()).found is False


@pytest.mark.parametrize(
    "local, llm, edited, keep",
    [
        (True, True, False, True),
        (True, False, False, True),
        (False, True, False, True),
        (False, False, False, False),
        (False, None, False, False),
        (False, False, True, True),
        (None, None, False, True),
        (None, False, False, False),
    ],
)
def test_photo_decision_matrix(local, llm, edited, keep):
    assert photo_decision(FaceCheck(local, 0.9, "détail"), llm, "", edited)[0] is keep


def test_pdf_photo_candidate_detected(tmp_path):
    buf = io.BytesIO()
    _badge().save(buf, "PNG")
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_image(pymupdf.Rect(40, 40, 160, 160), stream=buf.getvalue())
    for i, line in enumerate(["Jean DUPONT — Chef de projet SI", "Expérience : ACME Conseil, 2020 - 2024", "Compétences : SAP, Power BI"]):
        page.insert_text((40, 200 + 20 * i), line, fontsize=12)  # assez de texte : page non scannée
    pdf = tmp_path / "cv.pdf"
    doc.save(pdf)
    source = load_source(pdf)
    assert len(source.candidates) == 1 and source.text_is_reliable


# --- Typographie / mesure -------------------------------------------------------------------


def test_typography_helpers():
    assert contenu.tame_caps("CHEF DE PROJET SI JUNIOR") == "Chef de projet SI junior"
    assert contenu.tame_caps("PMO IT/IS CONTRAT EN ALTERNANCE") == "PMO IT/IS contrat en alternance"
    assert contenu.tame_caps("Expert MAXIMO/MAS") == "Expert MAXIMO/MAS"
    assert contenu.clean_item("gestion des sprints, etc.") == "Gestion des sprints, etc."
    assert contenu.clean_item("spaCy") == "spaCy"
    assert contenu.clean_item("dbt", capitalize=False) == "dbt"
    assert fix_text("Ing´enieur") == "Ingénieur"
    assert safe_name("CV - Expert MAXIMO/MAS") == "CV - Expert MAXIMO-MAS"


def test_measurement_and_split():
    para = Para([Run("mot " * 40, "regular", 10)], bullet="•", indent=0.4)
    assert line_count(para, 12.44) > 1
    long = Para([Run("mot " * 3000, "regular", 10)])
    pieces = _split_long_para(long, 12.44)
    assert len(pieces) > 1 and all(para_height(p, 12.44) <= 12.0 for p in pieces)


# --- Rendu PPTX ------------------------------------------------------------------------------


def _shape_texts(shapes):
    for sh in shapes:
        if sh.shape_type == 6:  # groupe (icône + titre de section)
            yield from _shape_texts(sh.shapes)
        elif sh.has_text_frame:
            yield sh.text_frame.text


def _all_text(prs) -> str:
    return " ".join(text for slide in prs.slides for text in _shape_texts(slide.shapes))


def test_render_one_page_and_fields(tmp_path):
    cv = make_cv()
    out = tmp_path / "cv.pptx"
    result = render_cv(cv, None, TEMPLATE_PATH, out, experience_label="5 ans d’expérience", contact=cv.contact.model_dump(), source_pages=1)
    prs = Presentation(str(out))
    text = _all_text(prs)
    assert result.pages == 1 and len(prs.slides) == 1
    assert "{{" not in text and "Jean DUPONT" in text and "SI & outils" in text and "5 ans d’expérience" in text
    rels = [r.target_ref for r in prs.slides[0].part.rels.values() if r.is_external]
    assert "mailto:jean.dupont@example.com" in rels and any("linkedin.com/in/jean-dupont" in r for r in rels)


def test_render_long_cv_paginates_only_experiences(tmp_path):
    cv = make_cv()
    cv.experiences = [cv.experiences[0].model_copy(update={"realisations": [f"Réalisation numéro {i} " * 6 for i in range(12)]}) for _ in range(8)]
    out = tmp_path / "long.pptx"
    result = render_cv(cv, None, TEMPLATE_PATH, out, experience_label=None, contact=cv.contact.model_dump(), source_pages=4)
    prs = Presentation(str(out))
    assert result.pages > 1
    for slide in list(prs.slides)[1:]:
        texts = " ".join(_shape_texts(slide.shapes))
        assert "Expériences (suite)" in texts and "Formation" not in texts and "Certifications" not in texts


# --- Anonymisation -----------------------------------------------------------------------------


def test_anonymisation_leaks_nothing():
    cv = make_cv()
    cv.experiences[0].realisations.append("Contact : jean.dupont@example.com, 06 12 34 56 78, Jean DUPONT")
    anon = anonymize(cv)
    dump = anon.model_dump_json()
    assert initials(cv) == "J. D."
    for leak in ("jean.dupont@example.com", "06 12 34 56 78", "DUPONT", "linkedin.com/in/jean-dupont"):
        assert leak not in dump
    assert anon.contact.localisation == "Lyon (69003)"


def test_extraction_request_shape():
    """L'appel GPT-6 Luna : PDF joint, store=False, sortie stricte, refus détectés."""
    from cv_formatter.extraction import ExtractionError, check_response

    with pytest.raises(ExtractionError):
        check_response(NS(status="incomplete", incomplete_details=NS(reason="max_output_tokens"), output=[]))
    with pytest.raises(ExtractionError):
        check_response(NS(status="completed", output=[NS(type="message", content=[NS(type="refusal", refusal="non")])]))
