"use client";

import type { AffidavitMetadata } from "@/lib/types";

interface Props {
  meta: AffidavitMetadata | null;
}

export default function AffidavitViewer({ meta }: Props) {
  if (!meta) {
    return (
      <div className="flex flex-col items-center justify-center h-48 rounded-2xl border border-dashed border-stone-300 bg-white text-stone-400 text-center gap-1 px-6">
        <span className="text-3xl">📝</span>
        <p className="text-sm">If the names on your documents don&apos;t match, a correction form will appear here.</p>
        <p className="text-sm">अगर आपके कागज़ों पर नाम अलग होगा, तो यहाँ सुधार फ़ॉर्म दिखेगा।</p>
      </div>
    );
  }

  // Build display rows from the backend `mismatches` records.
  const rows = (meta.mismatches ?? []).flatMap((m) => [
    { label: m.source_doc_label_en, name: m.source_name },
    { label: m.target_doc_label_en, name: m.target_name },
  ]);
  // De-duplicate by label.
  const seen = new Set<string>();
  const uniqueRows = rows.filter((r) => (seen.has(r.label) ? false : (seen.add(r.label), true)));

  return (
    <div className="flex flex-col gap-4">
      {/* Plain-language guidance (not printed) */}
      <div className="no-print rounded-2xl border border-amber-300 bg-amber-50 p-4 text-stone-800">
        <p className="text-base font-bold">⚠️ Your name is written differently on your documents.</p>
        <p className="text-sm mt-1 text-stone-600">आपके दस्तावेज़ों पर आपका नाम अलग-अलग लिखा है।</p>
        <ol className="mt-3 text-sm list-decimal list-inside space-y-1 text-stone-700">
          <li>Press <b>Print / Save</b> below and print this form.</li>
          <li>Buy a ₹{meta.stamp_paper_value_inr} stamp paper and write it on that.</li>
          <li>Get it signed by a Notary or Gazetted Officer.</li>
          <li>Take it to your nearest CSC / Nada Kacheri with your Aadhaar.</li>
        </ol>
      </div>

      <div className="flex items-center justify-between no-print">
        <h2 className="text-base font-bold text-stone-800">Name-Correction Form</h2>
        <button
          onClick={() => window.print()}
          className="px-5 py-2.5 rounded-xl bg-bharat-saffron text-white text-base font-bold hover:brightness-105 active:scale-95 transition"
        >
          🖨️ Print / Save
        </button>
      </div>

      {/* ── Print area ─────────────────────────────────────────────────────── */}
      <div
        id="affidavit-print-area"
        className="rounded-2xl border border-stone-300 bg-white text-black p-8 leading-relaxed text-sm"
        style={{ fontFamily: "Georgia, 'Times New Roman', serif" }}
      >
        <div className="text-center mb-6 border-b-2 border-black pb-4">
          <p className="text-xs tracking-widest uppercase">Non-Judicial Stamp Paper</p>
          <p className="text-lg font-bold mt-1">Value: Rs. {meta.stamp_paper_value_inr}/-</p>
          <p className="text-xs mt-1">
            Executed on stamp paper as required under the Indian Stamp Act, 1899.
          </p>
        </div>

        <h1 className="text-center font-bold text-base uppercase tracking-wide mb-1">
          {meta.document_title_en}
        </h1>
        <h1 className="text-center font-bold text-base tracking-wide mb-6">{meta.document_title_kn}</h1>

        <section className="mb-6">
          <p className="font-semibold mb-2 underline">English</p>
          <p className="mb-3 whitespace-pre-line">{meta.notary_sworn_text_en}</p>
          <p className="whitespace-pre-line">{meta.affidavit_body_en}</p>
        </section>

        <section className="mb-8">
          <p className="font-semibold mb-2 underline">ಕನ್ನಡ</p>
          <p className="mb-3 whitespace-pre-line">{meta.notary_sworn_text_kn}</p>
          <p className="whitespace-pre-line">{meta.affidavit_body_kn}</p>
        </section>

        {uniqueRows.length > 0 && (
          <section className="mb-8">
            <p className="font-semibold underline mb-2">Name Discrepancy Details</p>
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="border-b border-gray-400">
                  <th className="text-left py-1 pr-4">Document Type</th>
                  <th className="text-left py-1">Name as Found</th>
                </tr>
              </thead>
              <tbody>
                {uniqueRows.map((r, i) => (
                  <tr key={i} className="border-b border-gray-200">
                    <td className="py-1 pr-4">{r.label}</td>
                    <td className="py-1 font-medium">{r.name}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        )}

        <div className="grid grid-cols-2 gap-8 mt-10 pt-4 border-t border-gray-400">
          <div>
            <p className="font-semibold mb-16 text-xs">Deponent&apos;s Signature</p>
            <div className="border-t border-black pt-1 text-xs">
              <p>{meta.declarant_aadhaar_name}</p>
              <p className="text-gray-500">Case ID: {meta.case_id}</p>
              <p className="text-gray-500">Date: {meta.generated_at.substring(0, 10)}</p>
            </div>
          </div>
          <div>
            <p className="font-semibold mb-16 text-xs">Notary Public / Gazetted Officer</p>
            <div className="border-t border-black pt-1 text-xs">
              <p className="text-gray-500">Name &amp; Designation</p>
              <p className="text-gray-500">Registration No.</p>
              <p className="text-gray-500">Seal &amp; Date</p>
            </div>
          </div>
        </div>

        <p className="text-center text-xs text-gray-500 mt-8 border-t border-gray-300 pt-3">
          Generated by WelfareFlow India · {meta.generated_at}
        </p>
      </div>
    </div>
  );
}
