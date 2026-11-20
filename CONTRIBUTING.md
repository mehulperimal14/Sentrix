# Working on SENTRIX

One shared repository, `github.com/mehulperimal14/Sentrix`. Each person owns
an area (README "Team"; `.github/CODEOWNERS` lists the folders), works on a
branch, opens a pull request, and a teammate reviews it before it reaches
`main`. Every push to `main` redeploys the online Portal on Render, so `main`
only changes through reviewed pull requests.

## Once, before your first pull request

1. **Your email on GitHub.** Settings → Emails: add and verify the email you
   commit with, or use the private `…@users.noreply.github.com` address shown
   there. GitHub credits a commit only when its email belongs to your account.
2. **Accept the invite** to the repository (GitHub e-mails it, or see
   github.com/notifications).
3. **Install GitHub Desktop** (Windows or Mac), sign in, then File → Clone
   repository → `mehulperimal14/Sentrix`. The download is about 700 MB.
4. **Tell git who you are.** GitHub Desktop: Options → Git. Or in a terminal:

   ```
   git config --global user.name  "Your Name"
   git config --global user.email "the-email-from-step-1"
   ```

5. Set up Python and the app as in README "Quick start".

## How one change travels

1. **Issue.** A card on the Issues tab that says what needs doing and who
   does it, with an `area:` label and a milestone.
2. **Branch** from an up-to-date `main`, named `area/issue-number-short-name`:
   `frontend/12-loading-state`, `backend/15-ci-tests`, `ml/20-weapon-threshold`,
   `iot/22-led-bench`, `docs/9-team-pages`.
3. **Commits**, one change each, in the style the history already uses:
   `feat(portal): …`, `fix(link): …`, `docs: …`, `test: …`, `chore: …`.
4. **Pull request** into `main`. Fill in the template and write `Closes #12`
   so the issue closes itself. Keep it small (under about 400 changed lines).
5. **Checks.** GitHub runs the Python tests and the frontend type check and
   unit tests. A red check blocks the merge.
6. **Code review.** An owner of the changed folders reads it and approves or
   asks for changes; you answer with new commits on the same branch. Nobody
   approves their own pull request.
7. **Merge** with "Squash and merge"; GitHub deletes the branch.

## Code review

Start each comment with its weight: `blocking:` (must change before merging),
`suggestion:`, `question:`, `nit:` (tiny, optional), `praise:`. Review within
24 hours and resolve every conversation before merging.

A reviewer checks that the change:

- does what the issue says, and nothing else;
- has tests, and the checks are green;
- adds no `.env`, no `var/` files, no keys, no photos or videos of people;
- keeps test mode on: nothing can send a real alert;
- tunes or chooses nothing on the test video `wa02`;
- quotes model numbers from a generated report, with its commit;
- updates the docs when behaviour changes, with a screenshot for any screen.

## Rules that never change

- Never commit `.env`, `var/`, recordings (`*.avi`, `*.wav`) or `CustomVideos/`.
- Never let a test, evaluation, replay or demo send an alert (CLAUDE.md §1).
- Never open a webcam or microphone in tests or diagnostics.
- Decisions are numbered in CLAUDE.md §6 (D1, D2, …); a new one takes the next
  free number.

## Releases

`main` is tagged before each evaluation (`v1.0.0-rc.1`, `v1.0.0`) and the Pi is
updated from the tag, never from an unreviewed branch.
