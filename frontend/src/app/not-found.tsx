import type { Metadata } from "next";
import { ErrorScreen } from "@/components/ErrorScreen";
import { pageTitle } from "@/lib/page-title";

export const metadata: Metadata = { title: pageTitle("Хуудас олдсонгүй") };

export default function NotFound() {
  return (
    <ErrorScreen
      title="Хуудас олдсонгүй"
      hint="Хаяг буруу эсвэл энэ хуудас устсан байж магадгүй."
    />
  );
}
