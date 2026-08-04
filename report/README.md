# Project Report (LaTeX)

Follows the official Brainware University format
(`Project_Report_Template_BWU.docx`). This is a **living document** —
update it each review period rather than rewriting it.

## Structure
- `main.tex` — document class, packages, and assembly (also holds the
  project-wide variables like title, names, dates — edit these here)
- `titlepage.tex`, `certificate.tex`, `abstract.tex`, `symbols.tex`,
  `appendices.tex` — front/back matter
- `chapters/` — the five required chapters (Introduction, Literature
  Review, Methodology, Results, Conclusion)
- `references.bib` — bibliography (BibTeX, alphabetical by first author
  per the required format)
- `images/` — logo and any figures

## How to update for each review period
1. Update `\ReviewLabel` and `\SubmissionMonthYear` in `main.tex`
2. Edit the relevant chapter file(s) — most reviews will mainly touch
   `chapter4_results.tex` (new results) and `chapter5_conclusion.tex`
   (timeline status), while `chapter1`–`chapter3` stay largely stable
   after Review 1 unless the design itself changes
3. Recompile (see below) and check the rendered PDF before submitting

## Compiling
```bash
pdflatex main.tex
bibtex main
pdflatex main.tex
pdflatex main.tex
```
(Three pdflatex passes + one bibtex pass are needed for cross-references,
the table of contents, and the bibliography to resolve correctly.)

## Known template deviations (intentional)
The source `.docx` template has two layout defects that are **not**
replicated here:
1. The university header was orphaned alone on a near-blank page —
   fixed by keeping the title page as one continuous page.
2. The Bonafide Certificate's two signature columns were asymmetric
   (an extra line on one side pushed the rows out of alignment) —
   fixed to be row-symmetric between both columns.
