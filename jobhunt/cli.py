"""
Command-line interface and main entrypoint for the jobhunt tool.
Coordinates fetching, filtering, screening, drafting, and mailing.
Architecture note: Encapsulates the job search workflow in `JobPipeline` and exposes subcommands via `argparse`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

from . import digest as digest_mod
from . import llm, mailer
from .fetch import fetch_all
from .mock import fetch_all_mock
from .prefilter import prefilter
from .providers import LLMError, resolve
from .store import Store

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: str = ".env") -> None:
    """Minimal .env reader so there is no python-dotenv dependency."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _cfg(path: str | Path) -> dict:
    """Loads the YAML configuration file."""
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"config not found: {p}  (run from the project root)")
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def _load_profile(cfg: dict, allow_sample: bool) -> dict | None:
    """Loads the user's profile from disk, falling back to a sample if allowed."""
    path = Path(cfg.get("profile_file", "profile.json"))
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    sample = ROOT / "profile.example.json"
    if allow_sample and sample.exists():
        print(f"  ! {path} missing — using {sample.name} for this dry run.")
        print(
            "    Build the real one: python -m jobhunt profile --resume JobHunt_Resume.tex"
        )
        return json.loads(sample.read_text(encoding="utf-8"))

    print(f"missing {path} — run `python -m jobhunt profile --resume <file>` first")
    return None


def cmd_profile(args) -> int:
    """Command to extract and build a profile from a resume file."""
    src = Path(args.resume)
    if not src.exists():
        print(f"resume not found: {src}")
        return 1

    try:
        provider, model = resolve("draft")
        print(f"Loading {src.name}...")
        profile = llm.build_profile(
            resume_text=src.read_text(encoding="utf-8", errors="replace"),
            provider=provider,
            model=model,
        )
    except (LLMError, ValueError) as e:
        print(f"profile extraction failed: {e}")
        return 1

    Path(args.out).write_text(
        json.dumps(profile, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"wrote {args.out}\n")
    print(json.dumps(profile, indent=2, ensure_ascii=False)[:900])
    return 0


class JobPipeline:
    """Encapsulates the job hunting pipeline steps: fetch, filter, screen, and draft."""
    
    def __init__(self, args: argparse.Namespace, cfg: dict, profile: dict, store: Store):
        self.args = args
        self.cfg = cfg
        self.profile = profile
        self.store = store
        self.filters = cfg.get("filters", {}) or {}
        
        self.jobs = []
        self.scanned = 0
        self.candidates = 0
        self.shortlist = []
        self.attachments = []

    def execute(self) -> int:
        """Runs the entire pipeline sequentially."""
        if not self.fetch_jobs():
            return 1
            
        if not self.filter_jobs():
            self.generate_empty_digest()
            return 0
            
        if not self.screen_jobs():
            return 1
            
        self.draft_responses()
        self.finalize_digest()
        return 0

    def fetch_jobs(self) -> bool:
        print("\n[1/5] fetching boards")
        if self.args.mock:
            self.jobs = fetch_all_mock()
        else:
            companies = _cfg(self.cfg.get("companies_file", "companies.yaml")).get("companies") or []
            if not companies:
                print("companies.yaml has no entries")
                return False
            self.jobs = fetch_all(companies)
            
        self.scanned = len(self.jobs)
        if not self.scanned:
            print("no postings fetched — check the slugs in companies.yaml")
            return False
        return True

    def filter_jobs(self) -> bool:
        print("\n[2/5] filtering")
        self.jobs = prefilter(self.jobs, self.filters)
        self.passed_filters = len(self.jobs)
        self.jobs = self.store.unseen(self.jobs)
        
        print(f"  new since last run: {len(self.jobs)}")
        self.candidates = len(self.jobs)
        
        if self.args.limit:
            self.jobs = self.jobs[:self.args.limit]
            print(f"  --limit {self.args.limit} applied")
            
        return bool(self.jobs)

    def generate_empty_digest(self) -> None:
        subject, doc = digest_mod.build([], self.scanned, 0, self.store.stats())
        path = digest_mod.write(doc, self.cfg.get("digest_file", "out/digest.html"))
        print(f"\nnothing new today. preview: {path}")

    def screen_jobs(self) -> bool:
        scorer = "keyword" if self.args.scorer == "keyword" else "llm"
        
        if scorer == "keyword":
            print(f"\n[3/5] screening {len(self.jobs)} jobs (keyword stub — DEV ONLY)")
            llm.keyword_screen(self.jobs, self.profile)
        else:
            try:
                provider, model = resolve("screen")
            except LLMError as e:
                print(f"\n{e}\nNo key? Run with --scorer keyword for an offline dry run.")
                return False
                
            print(f"\n[3/5] screening {len(self.jobs)} jobs via {provider.name}/{model}")
            llm.screen(
                self.jobs,
                self.profile,
                batch_size=int(self.cfg.get("screen_batch_size", 8)),
                jd_chars=int(self.cfg.get("screen_jd_chars", 1400)),
                provider=provider,
                model=model,
            )

        if scorer == "llm" and not any(j.score is not None for j in self.jobs):
            print(
                "\n! screening scored nothing: every batch failed.\n"
                "  Not recording these jobs, so the next run retries them.\n"
                "  Check the warnings above (bad key, rate limit, wrong model id)."
            )
            return False

        threshold = float(self.cfg.get("score_threshold", 7.0))
        top_n = int(self.cfg.get("max_per_digest", 5))
        self.shortlist = sorted(
            [j for j in self.jobs if (j.score or 0) >= threshold],
            key=lambda j: j.score or 0,
            reverse=True,
        )[:top_n]
        print(f"  {len(self.shortlist)} scored >= {threshold}")
        return True

    def draft_responses(self) -> None:
        print(f"\n[4/5] drafting kits for {len(self.shortlist)}")
        scorer = "keyword" if self.args.scorer == "keyword" else "llm"
        
        if not self.shortlist:
            print("  nothing cleared the threshold")
            return
            
        if scorer == "keyword" or self.args.no_draft:
            print("  skipped (keyword scorer / --no-draft)")
            return

        try:
            provider, model = resolve("draft")
            print(f"  via {provider.name}/{model}")
            llm.draft(
                self.shortlist,
                self.profile,
                jd_chars=int(self.cfg.get("draft_jd_chars", 6000)),
                provider=provider,
                model=model,
            )
            self._draft_latex(provider, model)
        except LLMError as e:
            print(f"  ! drafting unavailable: {e}")

    def _draft_latex(self, provider, model) -> None:
        ref_path = Path(self.cfg.get("resume_file", "JobHunt_Resume.tex"))
        if not ref_path.exists():
            print(f"  ! reference latex {ref_path} not found, skipping latex drafts")
            return
            
        reference_tex = ref_path.read_text(encoding="utf-8")
        out_dir = Path("out")
        out_dir.mkdir(exist_ok=True)
        import subprocess

        for j in self.shortlist:
            print(f"  drafting latex for {j.title} @ {j.company}...")
            tex = llm.draft_latex(j, reference_tex, provider=provider, model=model)
            if tex:
                safe_name = f"{j.company}_{j.job_id}".replace(" ", "_").replace("/", "_")
                tex_file = out_dir / f"{safe_name}.tex"
                tex_file.write_text(tex, encoding="utf-8")
                
                res = subprocess.run(
                    [
                        "pdflatex",
                        "-interaction=nonstopmode",
                        f"-output-directory={out_dir}",
                        str(tex_file),
                    ],
                    capture_output=True,
                )
                pdf_file = out_dir / f"{safe_name}.pdf"
                if pdf_file.exists():
                    self.attachments.append(pdf_file)
                else:
                    print(f"  ! failed to compile {tex_file.name}, attaching .tex instead")
                    self.attachments.append(tex_file)

    def finalize_digest(self) -> None:
        print("\n[5/5] digest")
        subject, doc = digest_mod.build(self.shortlist, self.scanned, self.candidates, self.store.stats())
        path = digest_mod.write(doc, self.cfg.get("digest_file", "out/digest.html"))
        print(f"  wrote {path}")

        sent = False
        if self.args.send:
            try:
                mailer.send(subject, doc, attachments=self.attachments)
                sent = True
            except Exception as e:
                print(f"  ! email failed ({type(e).__name__}: {e}) — digest still on disk")
        else:
            print("  --send not passed, email skipped")

        self.store.record(self.jobs, emailed=sent)
        csv_path = self.store.export_csv(self.cfg.get("tracker_csv", "out/tracker.csv"))

        print(
            f"\nfunnel: {self.scanned} scanned -> {getattr(self, 'passed_filters', 0)} passed filters "
            f"-> {self.candidates} new -> {len(self.shortlist)} in digest"
        )
        print(f"subject: {subject}")
        print(f"tracker: {self.store.stats()}  ({csv_path})")


def cmd_run(args) -> int:
    """Command to execute the daily pipeline of fetching, filtering, screening, and drafting."""
    cfg = _cfg(args.config)
    profile = _load_profile(cfg, allow_sample=args.mock)
    if profile is None:
        return 1
    
    store = Store(cfg.get("seen_file", "seen.json"))
    pipeline = JobPipeline(args, cfg, profile, store)
    return pipeline.execute()


def cmd_applied(args) -> int:
    """Command to mark a specific job ID as applied."""
    store = Store(_cfg(args.config).get("seen_file", "seen.json"))
    ok = store.mark_applied(args.job_id)
    print("marked applied" if ok else f"unknown job_id: {args.job_id}")
    return 0 if ok else 1


def cmd_stats(args) -> int:
    """Command to print application tracking statistics and export to CSV."""
    cfg = _cfg(args.config)
    store = Store(cfg.get("seen_file", "seen.json"))
    print(json.dumps(store.stats(), indent=2))
    print(f"csv: {store.export_csv(cfg.get('tracker_csv', 'out/tracker.csv'))}")
    return 0


def main(argv=None) -> int:
    """Main entrypoint for the CLI application."""
    _load_env()
    p = argparse.ArgumentParser(
        prog="jobhunt",
        description="Personal job-search agent. Finds and drafts; never submits.",
    )
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("profile", help="turn a resume into profile.json")
    sp.add_argument(
        "--resume", required=True, help="path to a .tex, .txt or .md resume"
    )
    sp.add_argument("--out", default="profile.json")
    sp.set_defaults(func=cmd_profile)

    sr = sub.add_parser("run", help="run the daily pipeline")
    sr.add_argument("--mock", action="store_true", help="bundled fixtures, no network")
    sr.add_argument(
        "--scorer",
        choices=["llm", "keyword"],
        default="llm",
        help="keyword = offline stub, needs no API key",
    )
    sr.add_argument("--no-draft", action="store_true", help="skip the expensive stage")
    sr.add_argument("--send", action="store_true", help="actually email the digest")
    sr.add_argument("--limit", type=int, help="cap jobs sent to the LLM (cost guard)")
    sr.set_defaults(func=cmd_run)

    sa = sub.add_parser("applied", help="mark a job_id as applied")
    sa.add_argument("job_id")
    sa.set_defaults(func=cmd_applied)

    ss = sub.add_parser("stats", help="tracker summary + CSV export")
    ss.set_defaults(func=cmd_stats)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
