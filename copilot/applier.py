"""Application form copilot.

Modes (per platform, see config copilot.default_mode_by_platform):
  review_mode      fill everything, upload the tailored PDF, screenshot, then PAUSE before Submit. The browser
                   stays open; approve from the dashboard (or click Submit yourself) within the review window.
  auto_submit_ats  only on verified Greenhouse/Lever/Ashby forms, only when every required field has a
                   high-confidence mapping, only inside the execution window and under the daily cap.

Friction (OTP, CAPTCHA, login wall, unknown required question) always pauses, logs state + screenshot, and
surfaces in the Command Center. Nothing is ever retried around a friction point.

Can be run standalone:  python -m copilot.applier --job-id <id> [--mode review_mode|auto_submit_ats]
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from copilot.ats import adapter_for, adapter_for_url
from copilot.ats.greenhouse import embed_form_url
from copilot.ats.linkedin_easy_apply import LinkedInEasyApplyAdapter
from copilot.ats.naukri import NaukriAdapter
from copilot.browser import BrowserSession, first_visible, screenshot
from copilot.field_mapper import FieldMapper, FieldMapping, FormField, answer_question, summarize
from copilot.friction import Friction, detect_friction
from core import repository as repo
from core.browser_lock import browser_lock
from core.db import session_factory
from core.llm import LLMClient
from core.logging import log, setup_logging
from core.models import AppStatus, FormSessionStatus, FrictionType, JobStatus, platform_for
from core.profile import load_profile
from core.quota import QuotaManager
from core.schema import Job
from core.settings import Settings, load_settings

_NEXT_SELECTORS = [
    "button:has-text('Continue')",
    "button:has-text('Next')",
    "button[aria-label*='Next' i]",
    "a:has-text('Next step')",
]

REVIEW_POLL_SECONDS = 4


def _board_token(job: Job) -> str | None:
    try:
        return (json.loads(job.raw_metadata or "{}") or {}).get("board_token")
    except (ValueError, AttributeError):
        return None


@dataclass
class ApplyOutcome:
    job_id: str
    status: str  # FormSessionStatus value
    message: str
    friction: str | None = None
    screenshot: str | None = None
    field_map: list[dict[str, Any]] = field(default_factory=list)
    min_confidence: float | None = None


class Applier:
    def __init__(self, settings: Settings, browser: BrowserSession, llm: LLMClient | None = None) -> None:
        self.settings = settings
        self.browser = browser
        self.llm = llm
        self.profile = load_profile(settings.path(settings.app.profile_path))
        self.quota = QuotaManager(settings.quotas, settings.app.timezone)
        self.shots = settings.path(settings.app.screenshots_dir)

    # ------------------------------------------------------------------ public

    def apply(
        self,
        session: Session,
        job: Job,
        *,
        mode: str | None = None,
        enforce_window: bool = True,
        review_wait_seconds: int = 900,
    ) -> ApplyOutcome:
        platform = platform_for(job.source, job.ats_type)
        mode = mode or self.settings.copilot.default_mode_by_platform.get(platform, "review_mode")
        app = repo.ensure_application(session, job, mode=mode, status=AppStatus.READY_FOR_REVIEW)
        fs = repo.start_form_session(
            session, job_id=job.id, application_id=app.id, platform=platform, mode=mode
        )
        session.commit()

        if not self.quota.can_submit(session, platform):
            msg = f"daily cap reached for {platform}"
            repo.finish_form_session(
                session, fs, status=FormSessionStatus.ABANDONED.value, friction_payload={"reason": msg}
            )
            session.commit()
            return ApplyOutcome(job.id, FormSessionStatus.ABANDONED.value, msg)
        if enforce_window and not self.quota.in_execution_window():
            msg = f"outside execution window (next: {self.quota.next_window_start()})"
            repo.finish_form_session(
                session, fs, status=FormSessionStatus.ABANDONED.value, friction_payload={"reason": msg}
            )
            session.commit()
            return ApplyOutcome(job.id, FormSessionStatus.ABANDONED.value, msg)

        resume = repo.latest_resume(session, job.id)
        pdf = resume.pdf_path if resume else None
        adapter = adapter_for(job.ats_type, job.source)
        page = self.browser.new_page()
        url = job.apply_url
        if job.ats_type == "greenhouse":
            url = embed_form_url(url, _board_token(job))
        try:
            adapter.open(page, url)
            log.info("opened {} at {}", job.title, page.url)
            page.wait_for_timeout(1500)
            expect_login = platform in ("linkedin", "naukri")
            if fr := detect_friction(page, expect_logged_in=expect_login):
                return self._pause(session, fs, page, job, fr)

            if isinstance(adapter, NaukriAdapter):
                return self._naukri_apply(session, fs, page, job, adapter, pdf, review_wait_seconds)

            if isinstance(adapter, LinkedInEasyApplyAdapter):
                if not adapter.is_easy_apply(page):
                    return self._external_apply(session, fs, page, job, pdf, review_wait_seconds)
                return self._linkedin_flow(session, fs, page, job, adapter, pdf, mode, review_wait_seconds)

            return self._fill_and_finish(
                session, fs, page, job, adapter, pdf, mode, platform, review_wait_seconds, auto=False
            )
        except Exception as e:  # noqa: BLE001
            log.exception("applier failed for {}", job.id)
            shot = None
            try:
                shot = screenshot(page, self.shots, f"{job.id[:8]}_error")
            except Exception:  # noqa: BLE001
                pass
            repo.finish_form_session(
                session,
                fs,
                status=FormSessionStatus.FAILED.value,
                friction_payload={"error": str(e)},
                screenshot_path=shot,
            )
            session.commit()
            return ApplyOutcome(job.id, FormSessionStatus.FAILED.value, f"error: {e}", screenshot=shot)

    # ------------------------------------------------------------------ platform flows

    def _linkedin_flow(
        self, session, fs, page, job, adapter: LinkedInEasyApplyAdapter, pdf, mode, review_wait_seconds
    ) -> ApplyOutcome:
        if not adapter.reach_form(page):
            shot = screenshot(page, self.shots, f"{job.id[:8]}_li_nomodal")
            repo.finish_form_session(
                session,
                fs,
                status=FormSessionStatus.AWAITING_REVIEW.value,
                screenshot_path=shot,
                friction_payload={"reason": "Easy Apply modal did not open"},
            )
            session.commit()
            return ApplyOutcome(
                job.id,
                FormSessionStatus.AWAITING_REVIEW.value,
                "Easy Apply modal did not open; left for manual review",
                screenshot=shot,
            )
        mapper = self._mapper(session, pdf, job)
        all_mappings: list[FieldMapping] = []
        for _step in range(adapter.max_steps):
            fields = adapter.fields(page)
            mappings = mapper.map_fields(fields)
            all_mappings.extend(mappings)
            summary = summarize(mappings)
            if summary["unresolved_required"]:
                for m in mappings:
                    if m.field.required and m.value_source == "unresolved":
                        repo.add_screening_answer(
                            session,
                            job_id=job.id,
                            question_text=m.field.label,
                            matched_pattern=None,
                            answer=None,
                            confidence=0.0,
                            source="user",
                        )
                fr = Friction(
                    FrictionType.UNKNOWN_QUESTION,
                    "unresolved required fields: " + "; ".join(summary["unresolved_required"]),
                    page.url,
                )
                return self._pause(session, fs, page, job, fr, all_mappings)
            adapter.fill(page, mappings)
            page.wait_for_timeout(1200)
            if fr := detect_friction(page, expect_logged_in=True):
                return self._pause(session, fs, page, job, fr, all_mappings)
            state = adapter.advance(page)
            if state == "submit":
                adapter.uncheck_follow_company(page)
                break
            if state in ("stuck", "errors"):
                fr = Friction(
                    FrictionType.UNKNOWN_QUESTION, f"Easy Apply step could not advance ({state})", page.url
                )
                return self._pause(session, fs, page, job, fr, all_mappings)
        summary = summarize(all_mappings)
        app = repo.application_for_job(session, job.id)
        self.quota.human_delay("before submit")
        if adapter.submit(page):
            return self._applied(
                session, fs, app, job, "linkedin", page, all_mappings, summary, submitted_by="auto"
            )
        fr = Friction(
            FrictionType.UNKNOWN_QUESTION, "LinkedIn submit clicked but no confirmation detected", page.url
        )
        return self._pause(session, fs, page, job, fr, all_mappings)

    def _fill_and_finish(
        self,
        session,
        fs,
        page,
        job,
        adapter,
        pdf,
        mode,
        platform,
        review_wait_seconds,
        *,
        auto: bool,
        walk_steps: int = 1,
    ) -> ApplyOutcome:
        """Fill the form on `page`. `auto` submits when every required field came from the profile.

        `walk_steps` repeats fill-and-Next for company sites that spread the application across pages.
        """
        app = repo.application_for_job(session, job.id)
        if not adapter.reach_form(page):
            shot = screenshot(page, self.shots, f"{job.id[:8]}_noform")
            repo.finish_form_session(
                session,
                fs,
                status=FormSessionStatus.AWAITING_REVIEW.value,
                screenshot_path=shot,
                friction_payload={"reason": "form not found"},
            )
            session.commit()
            return ApplyOutcome(
                job.id,
                FormSessionStatus.AWAITING_REVIEW.value,
                "could not locate the application form; left open for manual review",
                screenshot=shot,
            )

        mapper = self._mapper(session, pdf, job)
        mappings: list[FieldMapping] = []
        earlier: list[FieldMapping] = []
        summary: dict[str, Any] = {"min_required_confidence": 1.0, "unresolved_required": []}
        for step in range(walk_steps):
            fields = adapter.fields(page)
            mappings = mapper.map_fields(fields)
            summary = summarize(mappings)
            failed = adapter.fill(page, mappings)
            page.wait_for_timeout(800)
            if fr := detect_friction(page, expect_logged_in=False):
                return self._pause(session, fs, page, job, fr, earlier + mappings)
            if summary["unresolved_required"] or failed:
                break
            nxt = first_visible(page, _NEXT_SELECTORS, 1000)
            if nxt is None or adapter.submit_button(page) is not None or step + 1 == walk_steps:
                break
            log.info("form step {} — clicking Next on {}", step + 1, page.url)
            earlier.extend(mappings)
            nxt.click()
            page.wait_for_timeout(1500)
        record = earlier + mappings

        if summary["unresolved_required"]:
            fr = Friction(
                FrictionType.UNKNOWN_QUESTION,
                "unresolved required fields: " + "; ".join(summary["unresolved_required"]),
                page.url,
            )
            for m in mappings:
                if m.field.required and m.value_source == "unresolved":
                    repo.add_screening_answer(
                        session,
                        job_id=job.id,
                        question_text=m.field.label,
                        matched_pattern=None,
                        answer=None,
                        confidence=0.0,
                        source="user",
                    )
            return self._pause(session, fs, page, job, fr, record)
        if failed:
            fr = Friction(
                FrictionType.UPLOAD_FAILED
                if any("resume" in f.lower() for f in failed)
                else FrictionType.UNKNOWN_QUESTION,
                "fill failed: " + "; ".join(failed),
                page.url,
            )
            return self._pause(session, fs, page, job, fr, record)

        needs_resume = any(m.field.kind == "file" for m in record)
        if needs_resume and not pdf:
            fr = Friction(FrictionType.UPLOAD_FAILED, "no tailored resume PDF for this job", page.url)
            return self._pause(session, fs, page, job, fr, record)
        confident = (
            summary["min_required_confidence"] >= self.settings.copilot.auto_submit.min_field_confidence
        )
        if auto:
            auto_ok = confident
        else:
            auto_ok = (
                mode == "auto_submit_ats"
                and adapter.verified_direct_ats
                and (job.ats_type in self.settings.copilot.auto_submit.allowed_ats)
                and confident
                and (pdf is not None or not needs_resume)
            )
        if auto_ok:
            self.quota.human_delay("before submit")
            if adapter.submit(page):
                return self._applied(
                    session, fs, app, job, platform, page, record, summary, submitted_by="auto"
                )
            fr = Friction(
                FrictionType.UNKNOWN_QUESTION, "submit clicked but no confirmation detected", page.url
            )
            return self._pause(session, fs, page, job, fr, record)
        return self._await_review(
            session, fs, app, job, platform, page, adapter, record, summary, review_wait_seconds
        )

    def _naukri_apply(
        self, session, fs, page, job, adapter: NaukriAdapter, pdf, review_wait_seconds
    ) -> ApplyOutcome:
        """Finish a Naukri application the user already approved. Stops only for a real blocker."""
        if adapter.already_applied(page):
            app = repo.application_for_job(session, job.id)
            return self._applied(
                session,
                fs,
                app,
                job,
                "naukri",
                page,
                [],
                {"min_required_confidence": 1.0},
                submitted_by="auto",
            )

        company = adapter.company_site_link(page)
        if company is not None:
            log.info("Naukri offers a company website for {}", job.company)
            return self._naukri_company_site(
                session, fs, page, job, adapter, company, pdf, review_wait_seconds
            )
        native = adapter.native_apply_button(page)

        if native is None:
            shot = screenshot(page, self.shots, f"{job.id[:8]}_noapply")
            repo.finish_form_session(
                session,
                fs,
                status=FormSessionStatus.AWAITING_REVIEW.value,
                screenshot_path=shot,
                friction_payload={"reason": "Apply button not found"},
            )
            session.commit()
            return ApplyOutcome(
                job.id,
                FormSessionStatus.AWAITING_REVIEW.value,
                "Naukri page has no Apply button; left open",
                screenshot=shot,
            )

        log.info("clicking Naukri Apply for {}", job.title)
        try:
            native.scroll_into_view_if_needed(timeout=5000)
            native.click()
        except Exception as e:  # noqa: BLE001
            log.warning("Naukri Apply click failed: {}", e)
            return self._pause(
                session,
                fs,
                page,
                job,
                Friction(FrictionType.UNKNOWN_QUESTION, f"could not click Apply: {e}", page.url),
            )
        page.wait_for_timeout(2000)
        if "naukri.com" not in (page.url or ""):
            log.info("Apply left Naukri for {}", page.url)
            return self._fill_and_finish(
                session,
                fs,
                page,
                job,
                adapter_for_url(page.url),
                pdf,
                "auto_submit_ats",
                "naukri",
                review_wait_seconds,
                auto=True,
                walk_steps=6,
            )
        if adapter.already_applied(page) or adapter.looks_submitted(page):
            app = repo.application_for_job(session, job.id)
            return self._applied(
                session,
                fs,
                app,
                job,
                "naukri",
                page,
                [],
                {"min_required_confidence": 1.0},
                submitted_by="auto",
            )
        if fr := detect_friction(page, expect_logged_in=True):
            return self._pause(session, fs, page, job, fr)
        for _ in range(8):
            if adapter.chat_open(page) or adapter.already_applied(page):
                break
            page.wait_for_timeout(1000)
        if adapter.already_applied(page) or adapter.looks_submitted(page):
            app = repo.application_for_job(session, job.id)
            return self._applied(
                session,
                fs,
                app,
                job,
                "naukri",
                page,
                [],
                {"min_required_confidence": 1.0},
                submitted_by="auto",
            )
        if adapter.chat_open(page) or adapter.form_present(page):
            return self._naukri_chat(session, fs, page, job, adapter)
        app = repo.application_for_job(session, job.id)
        return self._await_review(
            session,
            fs,
            app,
            job,
            "naukri",
            page,
            adapter,
            [],
            {"min_required_confidence": 1.0, "unresolved_required": []},
            review_wait_seconds,
        )

    def _naukri_company_site(
        self, session, fs, page, job, adapter: NaukriAdapter, link, pdf, review_wait_seconds
    ) -> ApplyOutcome:
        """Follow 'Apply on company website' and fill that form. Pauses on OTP, CAPTCHA, or unknown questions."""
        href = ""
        try:
            href = link.get_attribute("href") or ""
        except Exception:  # noqa: BLE001
            href = ""
        destination = page
        try:
            with page.context.expect_page(timeout=8000) as opened:
                link.click()
            destination = opened.value
            destination.wait_for_load_state("domcontentloaded", timeout=30_000)
        except Exception:  # noqa: BLE001
            page.wait_for_timeout(1500)
            if href.startswith("http") and "naukri.com" in (page.url or ""):
                page.goto(href, wait_until="domcontentloaded", timeout=45_000)
            destination = page
        destination.wait_for_timeout(1500)
        if fr := detect_friction(destination, expect_logged_in=False):
            return self._pause(session, fs, destination, job, fr)
        site = adapter_for_url(destination.url)
        log.info("company site {} via {}", destination.url, site.name)
        return self._fill_and_finish(
            session,
            fs,
            destination,
            job,
            site,
            pdf,
            "auto_submit_ats",
            "naukri",
            review_wait_seconds,
            auto=True,
            walk_steps=6,
        )

    def _naukri_chat(self, session, fs, page, job, adapter: NaukriAdapter) -> ApplyOutcome:
        """Answer the Naukri apply chat from the profile, one question at a time, until it shows Applied."""
        seen = ""
        sent: set[str] = set()
        stuck = 0
        approved = self._approved_answers(session)
        context = f"{job.title} at {job.company}. {(job.jd_text or '')[:3000]}"
        chat: list[FieldMapping] = []
        for _turn in range(12):
            if adapter.already_applied(page) or adapter.looks_submitted(page):
                app = repo.application_for_job(session, job.id)
                return self._applied(
                    session,
                    fs,
                    app,
                    job,
                    "naukri",
                    page,
                    chat,
                    {"min_required_confidence": 1.0},
                    submitted_by="auto",
                )
            if fr := detect_friction(page, expect_logged_in=True):
                return self._pause(session, fs, page, job, fr, chat)
            question = adapter.latest_question(page)
            # our own reply can render as the newest bubble before the next question arrives
            if not question or question == seen or question.strip().lower() in sent:
                stuck += 1
                log.debug("Naukri chat waiting for the next question (latest {!r})", question[:90])
                if stuck >= 4:
                    break
                page.wait_for_timeout(1000)
                continue
            stuck = 0
            seen = question
            options = adapter.reply_options(page)
            answer = answer_question(
                self.profile,
                self.settings.copilot.profile_field_map,
                question,
                options=options or None,
                llm=self.llm,
                approved_answers=approved,
                job_context=context,
            )
            log.info("Naukri chat {!r} -> {!r}", question[:90], answer)
            chat.append(
                FieldMapping(
                    FormField(
                        f"chat{len(chat)}", "text", question, required=True, options=list(options or [])
                    ),
                    answer,
                    "naukri_chat" if answer else "unresolved",
                    1.0 if answer else 0.0,
                )
            )
            if not answer:
                repo.add_screening_answer(
                    session,
                    job_id=job.id,
                    question_text=question,
                    matched_pattern=None,
                    answer=None,
                    confidence=0.0,
                    source="user",
                )
                return self._pause(
                    session,
                    fs,
                    page,
                    job,
                    Friction(
                        FrictionType.UNKNOWN_QUESTION,
                        "Naukri asked something not in your profile: " + question[:180],
                        page.url,
                    ),
                    chat,
                )
            if not adapter.answer_chat(page, answer):
                return self._pause(
                    session,
                    fs,
                    page,
                    job,
                    Friction(FrictionType.UNKNOWN_QUESTION, "could not send the chat answer", page.url),
                    chat,
                )
            sent.add(answer.strip().lower())
            page.wait_for_timeout(1500)
        if adapter.already_applied(page) or adapter.looks_submitted(page):
            app = repo.application_for_job(session, job.id)
            return self._applied(
                session,
                fs,
                app,
                job,
                "naukri",
                page,
                chat,
                {"min_required_confidence": 1.0},
                submitted_by="auto",
            )
        return self._pause(
            session,
            fs,
            page,
            job,
            Friction(FrictionType.UNKNOWN_QUESTION, "Naukri chat did not finish", page.url),
            chat,
        )

    def _external_apply(self, session, fs, page, job, pdf, review_wait_seconds) -> ApplyOutcome:
        """LinkedIn Apply that leaves LinkedIn: open the company site and walk its form."""
        link = first_visible(
            page,
            ["a:has-text('Apply')", "button:has-text('Apply')", ".jobs-apply-button"],
            3000,
        )
        if link is None:
            shot = screenshot(page, self.shots, f"{job.id[:8]}_external")
            repo.finish_form_session(
                session,
                fs,
                status=FormSessionStatus.AWAITING_REVIEW.value,
                screenshot_path=shot,
                friction_payload={"reason": "external apply link"},
            )
            session.commit()
            return ApplyOutcome(
                job.id,
                FormSessionStatus.AWAITING_REVIEW.value,
                "LinkedIn posting has no apply button; left open",
                screenshot=shot,
            )
        log.info("LinkedIn company-site apply for {}", job.company)
        return self._naukri_company_site(
            session, fs, page, job, NaukriAdapter(), link, pdf, review_wait_seconds
        )

    # ------------------------------------------------------------------ terminal states

    def _approved_answers(self, session: Session) -> dict[str, str]:
        return {
            a.question_text: a.answer
            for a in session.query(repo.ScreeningAnswer).filter_by(approved=1)
            if a.answer
        }

    def _mapper(self, session: Session, pdf: str | None, job: Job | None = None) -> FieldMapper:
        context = f"{job.title} at {job.company}. {(job.jd_text or '')[:3000]}" if job is not None else ""
        return FieldMapper(
            self.profile,
            self.settings.copilot.profile_field_map,
            llm=self.llm,
            approved_answers=self._approved_answers(session),
            resume_pdf=pdf,
            job_context=context,
        )

    def _pause(
        self, session, fs, page, job, fr: Friction, mappings: list[FieldMapping] | None = None
    ) -> ApplyOutcome:
        shot = (
            screenshot(page, self.shots, f"{job.id[:8]}_{fr.type}")
            if self.settings.copilot.screenshot_on_pause
            else None
        )
        fmap = [m.as_dict() for m in (mappings or [])]
        repo.finish_form_session(
            session,
            fs,
            status=FormSessionStatus.PAUSED_FRICTION.value,
            friction_type=fr.type.value,
            friction_payload={"detail": fr.detail, "url": fr.url},
            screenshot_path=shot,
            field_map=fmap,
        )
        session.commit()
        log.warning("PAUSED ({}) on {}: {}", fr.type, job.title, fr.detail)
        return ApplyOutcome(
            job.id,
            FormSessionStatus.PAUSED_FRICTION.value,
            fr.detail,
            friction=fr.type.value,
            screenshot=shot,
            field_map=fmap,
        )

    def _applied(
        self, session, fs, app, job, platform, page, mappings, summary, *, submitted_by: str
    ) -> ApplyOutcome:
        shot = screenshot(page, self.shots, f"{job.id[:8]}_submitted")
        fmap = [m.as_dict() for m in mappings]
        repo.finish_form_session(
            session,
            fs,
            status=FormSessionStatus.SUBMITTED.value,
            screenshot_path=shot,
            field_map=fmap,
            min_confidence=summary.get("min_required_confidence"),
        )
        repo.set_application_status(
            session,
            app,
            AppStatus.APPLIED,
            submitted_by=submitted_by,
            notes=f"submitted via copilot ({submitted_by})",
        )
        repo.set_job_status(session, job, JobStatus.APPLIED)
        self.quota.record_submission(session, platform)
        session.commit()
        return ApplyOutcome(
            job.id,
            FormSessionStatus.SUBMITTED.value,
            f"application submitted ({submitted_by})",
            screenshot=shot,
            field_map=fmap,
            min_confidence=summary.get("min_required_confidence"),
        )

    def _await_review(
        self, session, fs, app, job, platform, page, adapter, mappings, summary, wait_seconds: int
    ) -> ApplyOutcome:
        shot = screenshot(page, self.shots, f"{job.id[:8]}_review")
        fmap = [m.as_dict() for m in mappings]
        fs.status = FormSessionStatus.AWAITING_REVIEW.value
        fs.screenshot_path = shot
        fs.field_map = repo._dumps(fmap)
        fs.min_confidence = summary.get("min_required_confidence")
        session.commit()
        log.info(
            "AWAITING REVIEW: {} at {} - approve in the dashboard or submit manually in the browser",
            job.title,
            job.company,
        )
        # Only a change to "submitted" while we watch counts as your manual submit; a page that already
        # looked submitted when the review started must not be recorded as an application.
        baseline = adapter.looks_submitted(page)
        deadline = time.time() + wait_seconds
        while time.time() < deadline:
            time.sleep(REVIEW_POLL_SECONDS)
            session.expire(fs)
            decision = fs.status
            if decision == "submit_approved":
                self.quota.human_delay("before approved submit")
                if adapter.submit(page):
                    return self._applied(
                        session, fs, app, job, platform, page, mappings, summary, submitted_by="manual"
                    )
                fr = Friction(
                    FrictionType.UNKNOWN_QUESTION,
                    "approved submit clicked but no confirmation detected",
                    page.url,
                )
                return self._pause(session, fs, page, job, fr, mappings)
            if decision == FormSessionStatus.ABANDONED.value:
                repo.finish_form_session(
                    session,
                    fs,
                    status=FormSessionStatus.ABANDONED.value,
                    screenshot_path=shot,
                    field_map=fmap,
                )
                session.commit()
                return ApplyOutcome(
                    job.id,
                    FormSessionStatus.ABANDONED.value,
                    "abandoned from dashboard",
                    screenshot=shot,
                    field_map=fmap,
                )
            try:
                if page.is_closed():
                    break
                if not baseline and adapter.looks_submitted(page):
                    return self._applied(
                        session, fs, app, job, platform, page, mappings, summary, submitted_by="manual"
                    )
            except Exception:  # noqa: BLE001
                break
        # timed out: leave the session as awaiting_review so the dashboard can still mark it Applied manually
        repo.record_event(session, "form_session", fs.id, "review_timeout")
        session.commit()
        return ApplyOutcome(
            job.id,
            FormSessionStatus.AWAITING_REVIEW.value,
            "filled; waiting for your review (timed out polling)",
            screenshot=shot,
            field_map=fmap,
            min_confidence=summary.get("min_required_confidence"),
        )


# ---------------------------------------------------------------------- CLI


def run_for_job(
    job_id: str, mode: str | None = None, enforce_window: bool = False, review_wait_seconds: int = 900
) -> ApplyOutcome:
    settings = load_settings()
    setup_logging(settings.app.log_level)
    factory = session_factory(settings.path(settings.app.db_path))
    llm = LLMClient(settings.llm)
    with (
        browser_lock(settings, f"copilot {job_id[:8]}"),
        BrowserSession(settings) as browser,
        factory() as session,
    ):
        job = repo.get_job(session, job_id)
        if job is None:
            raise SystemExit(f"job {job_id} not found")
        return Applier(settings, browser, llm if llm.available else None).apply(
            session, job, mode=mode, enforce_window=enforce_window, review_wait_seconds=review_wait_seconds
        )


def main() -> None:
    ap = argparse.ArgumentParser(description="laa-pi application copilot")
    ap.add_argument("--job-id", required=True)
    ap.add_argument("--mode", choices=["review_mode", "auto_submit_ats"], default=None)
    ap.add_argument("--enforce-window", action="store_true")
    ap.add_argument("--review-wait", type=int, default=900)
    args = ap.parse_args()
    out = run_for_job(args.job_id, args.mode, args.enforce_window, args.review_wait)
    print(f"{out.status}: {out.message}")


if __name__ == "__main__":
    main()
