# V4.1
- Nhận link Facebook /share/... và tự resolve redirect sang URL thật.
- Tự suy ra Page/profile từ URL video đã resolve.
- Thử quét URL Page, /videos và /reels.
- Có fallback quét HTML công khai để tìm video ID khi yt-dlp không trả playlist.
- Giữ nguyên tải 1 video và batch ZIP tối đa 20 video/lượt.
- Không vượt đăng nhập/quyền riêng tư; Facebook có thể chặn server hoặc thay đổi HTML.
