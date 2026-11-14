#!/usr/bin/env python3
"""Extrai texto e imagens de um documento autorizado, preservando a localização.

Uso:
  python scripts/extract_source.py <arquivo> --project <slug>
  python scripts/extract_source.py <arquivo.pdf> --project <slug> --render-pages [--pages 3-10] [--dpi 150]
  python scripts/extract_source.py <arquivo.pdf> --project <slug> --clip PAGINA,x0,y0,x1,y1 [--dpi 200]

Formatos: PDF, DOCX, PPTX, TXT, Markdown, EPUB (via pandoc), imagens e APKG.

Saídas (o arquivo original nunca é alterado):
  projects/<slug>/source_text.json   texto por unidade (página, slide ou seção) + sha256 da fonte
  media/<slug>/                      imagens extraídas na resolução original
  projects/<slug>/pages/             páginas renderizadas, para leitura visual (--render-pages)
  sources/registry.json              registro das fontes autorizadas

Não há OCR neste ambiente. Páginas com pouco texto extraível entram em `low_text_units`
e precisam ser lidas visualmente a partir das páginas renderizadas.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEB_IMAGES = {"png", "jpg", "jpeg", "gif", "webp"}
MIN_SIDE = 80  # px; abaixo disso costuma ser ícone ou ornamento
LOW_TEXT = 40  # caracteres


def unit(page, section, text, images=None) -> dict:
    return {"page": page, "section": section, "text": text, "chars": len(text.strip()), "images": images or []}


def split_by_headings(text: str) -> list[dict]:
    """Divide texto corrido em seções a partir de títulos Markdown."""
    units, title, buf = [], None, []
    for line in text.splitlines():
        m = re.match(r"#{1,6}\s+(.*)", line)
        if m and "".join(buf).strip():
            units.append(unit(None, title, "\n".join(buf)))
            buf = []
        if m:
            title = m[1].strip()
        buf.append(line)
    units.append(unit(None, title, "\n".join(buf)))
    return units


def extract_pdf(path: Path, media: Path, rel) -> list[dict]:
    import pymupdf

    units, saved = [], {}
    with pymupdf.open(path) as doc:
        for number, page in enumerate(doc, 1):
            images = []
            for k, info in enumerate(page.get_images(full=True), 1):
                xref = info[0]
                if xref not in saved:
                    data = doc.extract_image(xref)
                    if min(data["width"], data["height"]) < MIN_SIDE:
                        continue
                    if data["ext"] in WEB_IMAGES and not info[1]:  # info[1] = máscara de transparência
                        target = media / f"p{number:04d}_img{k:02d}.{data['ext']}"
                        target.write_bytes(data["image"])
                    else:  # formatos que o Anki não exibe (jpx, jbig2, CMYK) ou com máscara: converte para PNG
                        pix = pymupdf.Pixmap(doc, xref)
                        if pix.colorspace and pix.colorspace.n > 3:
                            pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                        if info[1]:
                            try:
                                pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, info[1]))
                            except (RuntimeError, ValueError):
                                pass  # máscara incompatível: mantém a imagem sem transparência
                        target = media / f"p{number:04d}_img{k:02d}.png"
                        pix.save(target)
                    saved[xref] = {"file": rel(target), "width": data["width"], "height": data["height"]}
                rects = page.get_image_rects(xref)
                images.append({**saved[xref], "bbox_pt": [round(v, 1) for v in rects[0]] if rects else None})
            units.append(unit(number, None, page.get_text("text"), images))
    return units


def render_pdf(path: Path, out: Path, pages: list[int] | None, dpi: int) -> list[str]:
    import pymupdf

    out.mkdir(parents=True, exist_ok=True)
    written = []
    with pymupdf.open(path) as doc:
        for number in pages or range(1, doc.page_count + 1):
            target = out / f"p{number:04d}.png"
            doc[number - 1].get_pixmap(dpi=dpi).save(target)
            written.append(str(target))
    return written


def clip_pdf(path: Path, media: Path, spec: str, dpi: int) -> Path:
    """Recorta uma região da página (coordenadas em pontos do PDF). Serve para figuras vetoriais."""
    import pymupdf

    number, x0, y0, x1, y1 = (float(v) for v in spec.split(","))
    with pymupdf.open(path) as doc:
        target = media / f"p{int(number):04d}_clip_{int(x0)}_{int(y0)}_{int(x1)}_{int(y1)}.png"
        doc[int(number) - 1].get_pixmap(dpi=dpi, clip=pymupdf.Rect(x0, y0, x1, y1)).save(target)
    return target


def extract_pptx(path: Path, media: Path, rel) -> list[dict]:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    units = []
    for number, slide in enumerate(Presentation(str(path)).slides, 1):
        lines, images = [], []

        def walk(shapes):
            for shape in shapes:
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    walk(shape.shapes)
                elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    target = media / f"s{number:04d}_img{len(images) + 1:02d}.{shape.image.ext}"
                    target.write_bytes(shape.image.blob)
                    images.append({"file": rel(target), "width": shape.image.size[0], "height": shape.image.size[1]})
                elif shape.has_text_frame:
                    lines.extend(p.text for p in shape.text_frame.paragraphs)
                elif getattr(shape, "has_table", False) and shape.has_table:
                    lines.extend(" | ".join(c.text for c in row.cells) for row in shape.table.rows)

        walk(slide.shapes)
        if slide.has_notes_slide:
            lines.append(slide.notes_slide.notes_text_frame.text)
        title = slide.shapes.title.text if slide.shapes.title is not None else None
        units.append(unit(number, title, "\n".join(lines), images))
    return units


def extract_docx(path: Path, media: Path, rel) -> list[dict]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(str(path))
    lines = []
    for child in doc.element.body.iterchildren():
        if child.tag.endswith("}p"):
            para = Paragraph(child, doc)
            level = re.match(r"(?:Heading|Título|Title)\s*(\d)?", para.style.name or "")
            lines.append(("#" * int(level[1] or 1) + " " if level and para.text.strip() else "") + para.text)
        elif child.tag.endswith("}tbl"):
            lines.extend(" | ".join(c.text for c in row.cells) for row in Table(child, doc).rows)
    units = split_by_headings("\n".join(lines))
    images = []
    for part in doc.part.related_parts.values():
        if part.content_type.startswith("image/"):
            target = media / f"img{len(images) + 1:03d}{Path(part.partname).suffix}"
            target.write_bytes(part.blob)
            images.append({"file": rel(target)})
    units[0]["images"] = images  # o DOCX não informa a posição das imagens por seção
    return units


def extract_epub(path: Path, media: Path, rel) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        text = subprocess.run(
            ["pandoc", str(path), "-t", "markdown", "--wrap=none", f"--extract-media={tmp}"],
            check=True, capture_output=True, text=True,
        ).stdout
        images = []
        for src in sorted(p for p in Path(tmp).rglob("*") if p.suffix.lower().lstrip(".") in WEB_IMAGES):
            target = media / f"img{len(images) + 1:03d}{src.suffix.lower()}"
            shutil.copyfile(src, target)
            images.append({"file": rel(target)})
    units = split_by_headings(text)
    units[0]["images"] = images
    return units


def extract_apkg(path: Path, media: Path, rel) -> list[dict]:
    """Lê um baralho para análise ou adaptação: uma unidade por nota."""
    import os

    from anki.collection import Collection, ImportAnkiPackageRequest

    units = []
    with tempfile.TemporaryDirectory() as tmp:
        col = Collection(os.path.join(tmp, "in.anki2"))
        try:
            col.import_anki_package(ImportAnkiPackageRequest(package_path=str(path)))
            for nid in col.find_notes(""):
                note = col.get_note(nid)
                fields = "\n".join(f"{name}: {value}" for name, value in note.items())
                units.append(unit(None, f"{note.note_type()['name']} · {note.guid}", fields))
            for src in Path(col.media.dir()).iterdir():
                shutil.copyfile(src, media / src.name)
        finally:
            col.close()
    return units


def main() -> None:
    parser = argparse.ArgumentParser(description="Extrai texto e imagens de uma fonte autorizada")
    parser.add_argument("source", type=Path)
    parser.add_argument("--project", required=True, help="slug do projeto, ex.: bradiarritmias")
    parser.add_argument("--render-pages", action="store_true", help="renderiza páginas do PDF em PNG")
    parser.add_argument("--pages", help="intervalo para --render-pages, ex.: 3-10")
    parser.add_argument("--clip", help="recorta região do PDF: PAGINA,x0,y0,x1,y1 (pontos)")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args()

    src: Path = args.source.resolve()
    if not src.is_file():
        sys.exit(f"ERRO: arquivo não encontrado: {src}")
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.project):
        sys.exit("ERRO: --project deve usar apenas letras minúsculas, números, '-' e '_'")
    root: Path = args.root.resolve()
    project = root / "projects" / args.project
    media = root / "media" / args.project
    project.mkdir(parents=True, exist_ok=True)
    media.mkdir(parents=True, exist_ok=True)
    rel = lambda p: str(Path(p).relative_to(root))  # noqa: E731
    ext = src.suffix.lower().lstrip(".")

    if args.clip:
        print(rel(clip_pdf(src, media, args.clip, args.dpi)))
        return
    if args.render_pages:
        pages = None
        if args.pages:
            first, _, last = args.pages.partition("-")
            pages = list(range(int(first), int(last or first) + 1))
        written = render_pdf(src, project / "pages", pages, args.dpi)
        print(f"{len(written)} página(s) renderizada(s) em {rel(project / 'pages')}")
        return

    if ext == "pdf":
        units = extract_pdf(src, media, rel)
    elif ext == "pptx":
        units = extract_pptx(src, media, rel)
    elif ext == "docx":
        units = extract_docx(src, media, rel)
    elif ext in ("txt", "md", "markdown"):
        units = split_by_headings(src.read_text("utf-8", errors="replace"))
    elif ext == "epub":
        units = extract_epub(src, media, rel)
    elif ext == "apkg":
        if not zipfile.is_zipfile(src):
            sys.exit("ERRO: .apkg inválido")
        units = extract_apkg(src, media, rel)
    elif ext in WEB_IMAGES:
        target = media / src.name
        shutil.copyfile(src, target)
        units = [unit(1, None, "", [{"file": rel(target)}])]
    else:
        sys.exit(f"ERRO: formato não suportado: .{ext}")

    version = "sha256:" + hashlib.sha256(src.read_bytes()).hexdigest()
    source_name = rel(src) if src.is_relative_to(root) else str(src)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    low = [u["page"] or u["section"] or i for i, u in enumerate(units, 1) if u["chars"] < LOW_TEXT]
    dump = {
        "source_file": source_name, "source_version": version, "kind": ext, "extracted_at": now,
        "units": units, "low_text_units": low,
    }
    out = project / "source_text.json"
    out.write_text(json.dumps(dump, ensure_ascii=False, indent=1) + "\n", "utf-8")

    registry_path = root / "sources" / "registry.json"
    registry_path.parent.mkdir(exist_ok=True)
    registry = json.loads(registry_path.read_text("utf-8")) if registry_path.exists() else []
    registry = [r for r in registry if not (r["project"] == args.project and r["source_file"] == source_name)]
    registry.append({
        "project": args.project, "source_file": source_name, "source_version": version, "kind": ext,
        "bytes": src.stat().st_size, "units": len(units), "registered_at": now,
    })
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=1) + "\n", "utf-8")

    print(json.dumps({
        "source_file": source_name, "source_text": rel(out), "source_version": version, "units": len(units),
        "chars": sum(u["chars"] for u in units), "images": sum(len(u["images"]) for u in units),
        "low_text_units": low,
    }, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
