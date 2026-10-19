# Facebook Scraper

Ứng dụng web chạy cục bộ để thu thập bài đăng Facebook bằng Playwright, lưu dữ liệu trên máy và xuất JSON/CSV.

## Tính năng

- Nhập URL group, page, profile hoặc permalink; không cần từ khóa.
- Tách nội dung bài đăng khỏi bình luận; giữ liên kết bình luận và câu trả lời với bài gốc.
- Lưu ảnh và nhận dạng chữ bằng RapidOCR trên CPU.
- Đọc số cảm xúc theo từng loại khi Facebook cung cấp số liệu trực tiếp; không mở bảng chi tiết cảm xúc.
- Với group: lưu nội dung bài ngay, bổ sung bình luận theo từng đợt và cập nhật OCR sau.
- Giao diện cập nhật phần dữ liệu vừa lưu, không chờ hoàn tất bình luận hoặc OCR và không tải lại toàn bộ kho.
- Lưu SQLite, hàng đợi permalink và điểm phân trang để tiếp tục sau khi dừng hoặc khởi động lại.
- Bỏ qua các bài đã hoàn tất theo ID/URL; bài thiếu bình luận hoặc OCR có thể được xử lý lại.
- Xuất dữ liệu thành JSON, CSV bài đăng và CSV bình luận.

## Cài đặt trên Windows

Cần Python 3.10 trở lên. Từ thư mục dự án:

```powershell
cd web
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-local.txt
.\.venv\Scripts\python.exe -m playwright install chrome
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Điền `FACEBOOK_EMAIL` và `FACEBOOK_PASSWORD` trong `web/.env`. Tệp này chỉ nằm trên máy và được Git bỏ qua.

## Khởi động

Từ thư mục `web`:

```powershell
.\.venv\Scripts\python.exe local_server.py
```

Mở **http://127.0.0.1:4173**, nhập URL và chọn **Bắt đầu scrape**. Chrome sẽ mở để sử dụng phiên Facebook của bạn. Nếu Facebook yêu cầu xác minh, hoàn thành trong cửa sổ Chrome.

Dùng nút **Dừng lần chạy** để lưu phần đã đọc. Dừng server bằng `Ctrl+C`; khởi động lại bằng lệnh trên sau khi sửa mã hoặc `.env`.

## Cấu hình

Các thiết lập có trong [web/.env.example](web/.env.example):

| Biến | Ý nghĩa |
| --- | --- |
| `FACEBOOK_BROWSER_WORKERS` | Số trình đọc bài. Có thể dùng `1` để giảm số yêu cầu đồng thời. |
| `FACEBOOK_RESPONSE_READER` | `1`: đọc phản hồi Facebook đã xác minh; tự chuyển sang DOM khi không hỗ trợ. `0`: dùng DOM. |
| `FACEBOOK_REQUEST_INTERVAL_SECONDS` | Khoảng cách chủ động giữa các yêu cầu lấy dữ liệu, mặc định `0` (không chờ). |
| `FACEBOOK_COOLDOWN_SECONDS` | Cấu hình tạm nghỉ khi Facebook báo giới hạn. |
| `FACEBOOK_FEED_BATCH_SIZE` | Số bài đề nghị trên mỗi trang feed; Facebook có thể trả ít hơn. |
| `FACEBOOK_ASSET_CACHE` | Bộ đệm tài nguyên thử nghiệm, mặc định tắt. |

Ứng dụng tôn trọng `Retry-After` nếu có và lưu trạng thái giới hạn trong SQLite. Lỗi từ yêu cầu nền được phân loại riêng; thông tin chẩn đoán nằm trong `facebookCooldown.lastSignal` tại `/api/health`.

## Dữ liệu và tiếp tục scrape

| Vị trí | Nội dung |
| --- | --- |
| `web/data/group_pulse.sqlite3` | Bài đăng, bình luận, hàng đợi, điểm phân trang và lịch tạm nghỉ. |
| `web/data/media/` | Ảnh đã tải và bộ đệm OCR. |
| `web/.playwright-profile/` | Phiên đăng nhập Chrome trên máy. |

Giữ tùy chọn **Tiếp tục lịch sử từ điểm đã lưu** để khôi phục các permalink chưa đọc và chưa báo lỗi, rồi tiếp tục phân trang nếu điểm cũ còn tương thích. Bài chưa có trong kho được ưu tiên; các bài đã lưu nhưng thiếu dữ liệu và các permalink từng tải lỗi được giữ lại để thử khi đã duyệt hết phần lịch sử còn lại. Bỏ chọn để tìm bài từ đầu feed; dữ liệu đã lưu vẫn giữ nguyên và bài đã hoàn tất vẫn được bỏ qua.

Điểm phân trang gắn với tài khoản và chế độ sắp xếp. Đổi tài khoản có thể khiến điểm cũ không dùng được, dù kho dữ liệu và các permalink đang chờ vẫn còn. Chỉ sửa `.env` không tự thay đổi phiên Chrome đã đăng nhập.

Facebook quyết định thứ tự và phạm vi bài/bình luận được trả về. Số liệu chưa đọc được được giữ là chưa xác định, không mặc định bằng 0. Không có cam kết thu thập đủ mọi bài hoặc tránh được mọi giới hạn của Facebook.

## Cấu trúc mã nguồn

```text
exp/
├── LICENSE
├── README.md
└── web/
    ├── .env.example
    ├── requirements-local.txt
    ├── dist/                  # HTML, CSS, JavaScript của giao diện
    ├── local_server.py        # Server và API local
    ├── local_scraper.py       # Đọc nội dung bằng DOM
    ├── local_runner.py        # Điều phối hàng đợi và xử lý bài
    ├── local_workers.py       # Các trình đọc Playwright
    ├── local_feed.py          # Phân trang feed và tiếp tục lịch sử
    ├── local_response_reader.py
    ├── local_store.py         # SQLite và nhận diện bài đã lưu
    ├── local_tasks.py         # Hàng đợi bền vững
    ├── local_media.py         # Tải ảnh và OCR
    ├── local_throttle.py      # Giãn cách yêu cầu, lịch tạm nghỉ
    ├── local_profile.py       # Khóa phiên Chrome
    ├── local_discovery.py
    ├── local_capture.py
    ├── local_assets.py
    └── tests/                 # Kiểm thử bằng dữ liệu giả
```

Các thư mục dữ liệu, phiên Chrome, môi trường Python và `.env` được tạo cục bộ, không đưa lên GitHub. Thư mục `web/dist/` là mã giao diện cần được đưa lên Git.

## Kiểm thử

Từ thư mục `web`:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Các kiểm thử dùng cơ sở dữ liệu tạm và phản hồi giả; không scrape Facebook bằng tài khoản thật.

## Kết quả kiểm tra và tối ưu

### Kho dữ liệu

Số liệu được đọc từ SQLite trong bản dữ liệu tải về.

| Hạng mục | Kết quả |
| --- | --- |
| Bài đăng trong kho | 25.452 |
| Bình luận và câu trả lời trong kho | 135.718 |
| Lượt thả cảm xúc trên bài đăng đã ghi nhận | 211.903 |
| Lượt chia sẻ bài đăng đã ghi nhận | 8.754 |

Tổng cảm xúc và lượt chia sẻ được cộng từ số liệu đã đọc được trên bài đăng; các giá trị chưa xác định không được cộng vào tổng.

Các bài chưa hoàn tất được giữ lại để có thể xử lý tiếp. Mã nguồn nằm trong repo; cơ sở dữ liệu, media và bộ đệm OCR được cung cấp qua GitHub Release. Thông tin đăng nhập Facebook và phiên Chrome được giữ trên máy.

### Tải dữ liệu

[Tải ZIP kho dữ liệu](https://github.com/Bwada3/uit-group-crawler/releases/download/data-snapshot-20261019-231940/uit-group-crawler-data.zip) · [Xem Release](https://github.com/Bwada3/uit-group-crawler/releases/tag/data-snapshot-20261019-231940)

ZIP gồm SQLite, media và bộ đệm OCR, giữ cấu trúc `web/data/`. Để khôi phục, dừng server, chuyển thư mục `web/data` đang có sang nơi sao lưu rồi giải nén ZIP vào thư mục gốc dự án. File `.env` và thư mục `.playwright-profile` không nằm trong ZIP.

### Lưu dữ liệu trong lúc scrape

- Đã xác minh nội dung bài được ghi xuống SQLite trước khi hoàn tất bình luận và OCR; bình luận được lưu theo từng đợt.
- Thử cho tiến trình thoát khi bài chưa hoàn tất: nội dung và bình luận đã ghi vẫn đọc lại được; bài chưa hoàn tất vẫn có thể được xử lý tiếp.
- Giao diện nhận phần thay đổi trong lúc chạy, không tải lại toàn bộ kho.
- JSON thô chỉ được dựng khi mở tab tương ứng, tránh xử lý toàn bộ dữ liệu khi tab đang ẩn.
- Thử thay đổi cấu trúc SQLite trên bản sao kho thật: nội dung cũ được giữ nguyên. Việc kiểm tra không thay đổi kho gốc hoặc điểm tiếp tục phân trang.

## Nguồn tham khảo

Phát triển từ dự án [Ibrahimghali/Facebook-Scraper](https://github.com/Ibrahimghali/Facebook-Scraper). Giữ nguyên tệp [LICENSE](LICENSE) của dự án. Chỉ thu thập nội dung bạn có quyền truy cập và tuân thủ điều khoản, quyền riêng tư liên quan.
