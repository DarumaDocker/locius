---
name: pdf-forms
description: 填写 PDF 表格：邮件附件里的回执、同意书、申请表 → 填好副本 → 给用户检查 → 批准后回复邮件 Fill PDF forms (permission slips, consent and application forms) from email attachments, locally, and reply with the filled copy after review.
---
# PDF forms / 填写 PDF 表格

Everything happens on this Olares — never upload the user's documents to online converters or "free PDF filler" sites.

1. **Find the form.** gmail_search (e.g. `has:attachment filename:pdf newer_than:30d` plus the school/company name),
   gmail_get_message to see the attachment names, then gmail_save_attachment(message_id, filename) → `attachments/…pdf`.
   (Or the user put the PDF in the workspace — files_list.)
2. **Read the fields.** pdf_form_fields(path). No fields = it's a scan/flat PDF: tell the user; offer to write the answers
   as a document (make_pdf) instead of pretending to fill it.
3. **Collect the values.**
   - Use only facts the user gave you in this chat, facts stated in the email itself (e.g. the trip date), and memories the
     user explicitly saved (memory_search). Never invent names, phone numbers, IDs, allergies or medical details.
   - Ask ONE short question listing every missing field (e.g. "emergency phone? lunch: regular or vegetarian?").
   - Signature fields: leave empty — the user signs. Don't type ID/passport/bank numbers unless the user typed them
     to you for this form.
4. **Fill a copy.** pdf_form_fill(path, values) → `…-filled.pdf` (original untouched). Read `problems` and
   `required_still_empty`; fix field names or ask the user.
5. **Show it.** send_file the filled PDF with a short list of what you filled. Wait for the user's OK or corrections
   (then fill again).
6. **Send (only if asked).** gmail_reply(message_id, body, attachments=["attachments/…-filled.pdf"]) — the approval card
   shows the attachment. Mention anything the user still has to do (sign, pay, bring something).
