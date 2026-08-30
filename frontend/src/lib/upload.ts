/**
 * Direct-to-R2 upload.
 *
 * The file goes straight from the browser to object storage on a presigned
 * URL; it never passes through the API. A multi-gigabyte video through a
 * dyno is a request timeout and a doubled bandwidth bill.
 *
 * XMLHttpRequest rather than fetch, purely for `upload.onprogress`: fetch has
 * no upload-progress event, and a 4 GB upload with no visible progress is
 * indistinguishable from a hang.
 */

export interface UploadHandle {
  promise: Promise<void>;
  abort: () => void;
}

export function uploadToStorage(
  url: string,
  file: File,
  onProgress: (fraction: number) => void,
): UploadHandle {
  const xhr = new XMLHttpRequest();

  const promise = new Promise<void>((resolve, reject) => {
    xhr.open("PUT", url, true);
    // Must match the Content-Type the URL was signed with, or the signature
    // check fails with a 403 that says nothing about why.
    xhr.setRequestHeader("Content-Type", file.type || "application/octet-stream");

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        onProgress(1);
        resolve();
      } else if (xhr.status === 403) {
        reject(
          new Error(
            "Хуулах холбоос хүчингүй болсон байна. Хуудсыг сэргээгээд дахин оролдоно уу.",
          ),
        );
      } else {
        reject(new Error(`Хадгалах сан файлыг татгалзлаа (${xhr.status}).`));
      }
    };

    xhr.onerror = () => reject(new Error("Сүлжээний алдаа: файл бүрэн хуулагдсангүй."));
    xhr.onabort = () => reject(new Error("Хуулалт цуцлагдлаа."));

    xhr.send(file);
  });

  return { promise, abort: () => xhr.abort() };
}
