import { ErrorScreen } from "@/components/ErrorScreen";

export default function NotFound() {
  return (
    <ErrorScreen
      title="Хуудас олдсонгүй"
      hint="Хаяг буруу эсвэл энэ хуудас устсан байж магадгүй."
    />
  );
}
