import { describe, expect, it } from "vitest";

import type { DocumentType } from "./api";
import { displayName, matching, pageCount, since, sizeLabel, splitName } from "./lib";
import { cell, fieldLabel, isListField, itemColumns, tableRows, tableValues } from "./tables";

describe("names and sizes", () => {
  it("removes the upload prefix", () => {
    expect(displayName(`${"a".repeat(32)}_pay_slip.pdf`)).toBe("pay_slip.pdf");
    expect(displayName("pay_slip.pdf")).toBe("pay_slip.pdf");
    expect(splitName("a.b.pdf")).toEqual({ stem: "a.b", suffix: ".pdf" });
    expect(sizeLabel(1536)).toBe("2 KB");
    expect(pageCount(13, 12)).toBe(2);
  });
});

describe("search", () => {
  const documents = [
    { name: "a.pdf", title: "March pay", issuer: "Acme", doc_type: "bank_statement", financial_year: "2025-26", tags: ["salary"] },
    { name: "b.pdf" },
  ];

  it("looks at the name, title, issuer, type, year, and tags", () => {
    for (const query of ["a.pdf", "MARCH", "acme", "bank statement", "2025", "salary"]) {
      expect(matching(documents, query)).toHaveLength(1);
    }
    expect(matching(documents, "zzz")).toHaveLength(0);
    expect(matching(documents, " ")).toHaveLength(2);
  });
});

describe("elapsed time", () => {
  const start = "2026-01-01T00:00:00Z";
  it("reads naturally at each scale", () => {
    expect(since(start, "2026-01-01T00:00:09Z")).toBe("9s");
    expect(since(start, "2026-01-01T00:03:05Z")).toBe("3m 05s");
    expect(since(start, "2026-01-01T02:03:05Z")).toBe("2h 03m");
    expect(since(null)).toBe("");
  });
});

describe("detail tables", () => {
  it("round-trips object rows, dropping blank ones", () => {
    const columns = ["label", "amount"];
    const rows = tableRows([{ label: "Basic", amount: 50000 }, { label: "HRA", amount: 20000.5 }], columns);
    expect(rows[1]).toEqual({ label: "HRA", amount: "20000.5" });
    rows[1].amount = " 21,000 ";
    rows.push({ label: "", amount: " " });
    expect(tableValues(rows, columns)).toEqual([
      { label: "Basic", amount: "50000" },
      { label: "HRA", amount: "21,000" },
    ]);
  });

  it("uses one column for plain lists", () => {
    const rows = tableRows(["Python", "Docker"], null);
    rows.push({ Value: "" });
    expect(tableValues(rows, null)).toEqual(["Python", "Docker"]);
  });

  it("handles missing lists and values", () => {
    expect(tableRows(null, ["date"])).toEqual([]);
    expect(cell(null)).toBe("");
    expect(cell(0)).toBe("0");
    expect(fieldLabel("financial_year")).toBe("Financial year");
  });

  it("reads list columns through schema references", () => {
    const type = {
      doc_type: "payslip",
      label: "Payslip",
      fields: {
        $defs: { LineItem: { properties: { label: {}, amount: {} } } },
        properties: {
          earnings: { anyOf: [{ type: "array", items: { $ref: "#/$defs/LineItem" } }, { type: "null" }] },
          skills: { type: "array", items: { type: "string" } },
          employer: { anyOf: [{ type: "string" }, { type: "null" }] },
        },
      },
    } as unknown as DocumentType;
    expect(itemColumns(type, "earnings")).toEqual(["label", "amount"]);
    expect(itemColumns(type, "skills")).toBeNull();
    expect(isListField(type.fields!.properties!.earnings)).toBe(true);
    expect(isListField(type.fields!.properties!.employer)).toBe(false);
  });
});
