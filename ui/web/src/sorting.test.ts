import { describe, expect, it } from "vitest";

import type { DocumentRecord } from "./api";
import { SORTS, sortItems } from "./sorting";

describe("gallery sort orders", () => {
  const item = (name: string, fields: Partial<DocumentRecord>) => ({ name, size_bytes: 1, ...fields }) as DocumentRecord;
  const items = [
    item("payslip_mar.pdf", { title: "Acme payslip, March", doc_type: "payslip", document_date: "2025-03-31", created_at: "2026-01-05T00:00:00Z", page_count: 1, size_bytes: 50 }),
    item("lab_report.pdf", { title: "Tata 1mg lab report", doc_type: "health_report", document_date: "2026-04-18", created_at: "2026-11-26T00:00:00Z", page_count: 30, size_bytes: 2000 }),
    item("note.pdf", { title: null, doc_type: "other", document_date: null, created_at: "2025-12-01T00:00:00Z", page_count: null, size_bytes: 10 }),
  ];
  const names = (id: string) => sortItems(items, SORTS, id).map((document) => document.name);
  it("keeps similarity by default and sorts by when added, document date, title, type, size, and pages", () => {
    expect(sortItems(items, SORTS, "similarity")).toBe(items);
    expect(names("newest")).toEqual(["lab_report.pdf", "payslip_mar.pdf", "note.pdf"]);
    expect(names("dated-newest")).toEqual(["lab_report.pdf", "payslip_mar.pdf", "note.pdf"]);
    expect(names("dated-oldest")).toEqual(["payslip_mar.pdf", "lab_report.pdf", "note.pdf"]);
    expect(names("title")[0]).toBe("payslip_mar.pdf");
    expect(names("type")).toEqual(["lab_report.pdf", "note.pdf", "payslip_mar.pdf"]);
    expect(names("largest")).toEqual(["lab_report.pdf", "payslip_mar.pdf", "note.pdf"]);
    expect(names("pages")).toEqual(["lab_report.pdf", "payslip_mar.pdf", "note.pdf"]);
  });
});
