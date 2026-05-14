# BayouBombersApp

Responsive MVP web application for Bayou Bombers throws training operations, implemented in Python.

## Run

```bash
cd <project-directory>
python3 app.py
```

Then open `http://127.0.0.1:8000`.

## Home Experience

- Home page now shows a full-cover hero using the admin-managed background image and modern top navigation.
- Clicking the Bayou Bombers site name in the top bar always returns to the home page.
- Public search supports coach/athlete profile discovery with privacy filtering.
- Admin can update About text, home cover image, and account creation policy.
- Accounts sign in with email, while optional handles remain available for public profiles.
- Public profiles include gallery photos plus a status feed, and signed-in users can manage profile details from their profile tools.

## Included MVP Areas

- Authentication with coach and athlete roles
- Athlete setup (sex, events, group)
- Training module library
- Practice plan creation and assignment
- Athlete daily checkoff flow
- Throw, lift, and meet data logging
- Automatic PR updates
- Coach dashboard (completion + throw alerts)
- Weekly reporting summary
