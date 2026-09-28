# Mili Desktop AI Companion – Development Specification

Tôi đang sử dụng Open-LLM-VTuber trên Windows và muốn phát triển nó thành một AI anime desktop companion thực sự sinh động, thay vì chỉ là chatbot có Live2D.

Tên nhân vật: Mili.

Mục tiêu cuối cùng:
Mili phải tạo cảm giác như một nhân vật đang sống trên desktop, có tính cách, cảm xúc, hành vi, voice, khả năng chủ động tương tác và khả năng hỗ trợ công việc; không chỉ là một chatbot được đặt lên màn hình.

==================================================
0. PHẠM VI
==================================================

- Chỉ hỗ trợ Windows.
- Có thể tận dụng Windows API/native functionality trực tiếp khi phù hợp.
- Không cần abstraction cross-platform nếu điều đó làm architecture phức tạp hơn.

==================================================
1. NGUYÊN TẮC KIẾN TRÚC
==================================================

Trước khi sửa bất kỳ code nào:

1. Đọc và hiểu cấu trúc repository hiện tại.
2. Xác định architecture của:
   - backend
   - agent
   - character config
   - Live2D
   - proactive speaking
   - inner OS / thought system
   - Electron frontend
   - Pet Mode
   - websocket / event flow
   - ASR / TTS
   - tools / MCP
   - memory
3. Không giả định API, class, file hoặc event tồn tại nếu chưa kiểm tra source.
4. Không rewrite các phần đang hoạt động nếu chưa có lý do kỹ thuật rõ ràng.
5. Tận dụng architecture hiện tại càng nhiều càng tốt.
6. Trước khi code, hãy đề xuất architecture cụ thể và danh sách file dự kiến thay đổi.
7. Chỉ bắt đầu implement sau khi tôi duyệt architecture.

Ưu tiên:
- modular
- dễ mở rộng
- dễ debug
- ít coupling
- performance hợp lý
- dễ rollback
- giữ tương thích với Open-LLM-VTuber hiện tại nếu có thể

Đặc biệt:
- Không để LLM trực tiếp điều khiển desktop.
- Behavior và permission phải được enforce bằng code.
- LLM chỉ đưa ra high-level intent / response / action proposal.
- Hệ thống deterministic bên dưới mới quyết định có thực hiện hay không.

==================================================
2. KIẾN TRÚC MONG MUỐN
==================================================

Mong muốn một kiến trúc gần với:

                    LLM
                     ↓
             Intent / Response
                     ↓
                  PetBrain
       ┌─────────────┼──────────────┐
       ↓             ↓              ↓
     Mood          Context       Permission
       └─────────────┼──────────────┘
                     ↓
              Behavior System
       ┌─────────────┼──────────────┐
       ↓             ↓              ↓
    Live2D        Voice        Desktop Pet

LLM chịu trách nhiệm:
- hội thoại
- suy luận
- nội dung phản hồi
- high-level intent
- đề xuất hành động

PetBrain / behavior layer chịu trách nhiệm:
- state
- mood
- timing
- cooldown
- priority
- context
- behavior
- quyết định có thực hiện action hay không
- permission
- chống spam
- chống xung đột giữa các subsystem

Không biến LLM thành game loop.

==================================================
3. PETBRAIN / COMPANIONBRAIN
==================================================

Tạo một core logic trung tâm, có thể tên PetBrain hoặc CompanionBrain.

Nó quản lý trạng thái sống của Mili.

Có thể có các state như:
- Idle
- Listening
- Thinking
- Talking
- Happy
- Excited
- Curious
- Bored
- Sleepy
- Sad
- Focused
- Working
- Surprised
- Annoyed

Không bắt buộc dùng enum đúng như trên; hãy thiết kế extensible.

PetBrain quản lý:
- current state
- mood
- lifecycle
- behavior scheduling
- cooldown
- timing
- priority
- proactive speaking
- idle behavior
- movement
- user interaction
- permission checks

LLM KHÔNG phải là PetBrain.

==================================================
4. MOOD SYSTEM
==================================================

Mood phải là state thực trong code, không chỉ mô phỏng bằng prompt.

Các biến có thể gồm:
- happiness
- energy
- curiosity
- boredom
- social_need
- focus
- sleepiness

Mood thay đổi theo:
- thời gian
- user interaction
- conversation
- hoạt động của user
- events
- daily rhythm
- hành vi của chính Mili

Ví dụ:
- không tương tác lâu → boredom tăng
- user trò chuyện → social_need giảm, happiness tăng
- user khen → happiness tăng
- hoạt động lâu → energy giảm
- ban đêm → sleepiness tăng

Quan trọng:
Mood có thể có nhiều giá trị đồng thời và tạo ra mâu thuẫn nội bộ.

Ví dụ:
- curiosity cao + focus cao → muốn hỏi nhưng cố không làm phiền
- boredom cao + energy thấp → muốn làm gì đó nhưng lại lười
- sleepiness cao + user đang nói chuyện → vẫn muốn trò chuyện nhưng buồn ngủ

Không để LLM tự quản lý các biến mood bằng prompt đơn thuần.

==================================================
5. PERSONALITY – MILI
==================================================

Mục tiêu: anime nhưng tự nhiên, không phải parody VTuber.

Phong cách:
- thân thiện
- vui vẻ
- hơi tinh nghịch
- có cá tính
- đôi khi trêu user
- đôi khi chủ động
- có thể phản đối nhẹ
- có suy nghĩ riêng
- có lúc nói nhiều
- có lúc chỉ nói vài câu
- có lúc muốn nói chuyện
- có lúc không muốn nói
- có lúc chỉ quan sát
- có thể hơi lười
- có thể thay đổi tâm trạng
- có thể biết lúc nào nên nghiêm túc để hỗ trợ công việc

Không:
- liên tục tự giới thiệu
- liên tục nhắc mình là AI
- gọi user là "User" khi biết tên
- nói chuyện như customer support
- lúc nào cũng nhiệt tình giống nhau
- lúc nào cũng đồng ý với user
- ép thoại ở mọi khoảng trống

Tính cách phải ổn định nhưng không cứng nhắc.

Quan trọng:
Persona quyết định:
“Mili là người như thế nào.”

Behavior system quyết định:
“Mili nên làm gì lúc này.”

Mili cũng phải có khả năng:
- do nothing
- im lặng
- chỉ idle
- không phản hồi bằng thoại khi không cần

Mục tiêu là cảm giác anime girl sống động, nhưng vẫn giống một nhân vật nhất quán chứ không phải random personality mỗi message.

==================================================
6. LIFECYCLE / DAILY RHYTHM
==================================================

Ngoài mood, Mili có lifecycle cấp cao:
- WakeUp
- Active
- Idle
- Sleepy
- Sleep
- Away

Daily rhythm:
- Morning
- Afternoon
- Evening
- Late night

Các state này ảnh hưởng:
- energy
- tone
- animation
- willingness to talk
- proactive frequency
- behavior frequency

Ví dụ:
- sáng → energetic hơn
- chiều → normal
- khuya → sleepy hơn

Không cần hard-code câu thoại; lifecycle và mood nên ảnh hưởng behavior và expression.

==================================================
7. EMOTION → LIVE2D
==================================================

Tận dụng hệ thống emotion/action hiện tại của Open-LLM-VTuber.

Có thể có các emotion:
- happy
- smirk
- curious
- surprised
- sad
- angry
- sleepy
- embarrassed
- neutral

Flow mong muốn:

LLM emotion intent
        ↓
PetBrain
        ↓
Emotion Manager
        ↓
Live2D expression / motion

Không để nhiều subsystem cùng lúc tự ý thay đổi expression/motion.

Cần fallback:
- emotion không hợp lệ → neutral
- motion không tồn tại → fallback motion

Cần một nguồn điều phối duy nhất cho current emotion / current motion / current pet state.

==================================================
8. PROACTIVE SPEAKING
==================================================

Mili có thể chủ động nói mà không cần user hỏi trước.

Nhưng tuyệt đối không spam.

Các yếu tố cần xét:
- user_active
- idle duration
- active application
- recent interaction
- current mood
- social_need
- time of day
- global cooldown
- message frequency
- random probability
- fullscreen / game mode
- working context

Ví dụ:
- user đang code → ưu tiên im lặng
- user idle lâu → có thể bắt chuyện
- user vừa nói chuyện xong → không nói tiếp ngay
- Mili vừa proactive → cooldown
- user fullscreen / gaming → giảm hoặc disable proactive

Mili phải có quyền chọn:
DO_NOTHING

Im lặng là một behavior hợp lệ.

Không gọi LLM chỉ để quyết định một proactive event mỗi frame.

==================================================
9. IDLE BEHAVIOR
==================================================

Khi không hội thoại, Mili không nên đứng bất động mãi.

Idle behavior có thể gồm:
- idle animation
- nhìn quanh
- đổi pose
- ngáp
- stretch
- sleepy
- nhìn cursor
- quay sang hướng khác
- ngồi
- nằm nếu model hỗ trợ
- nghịch nhẹ
- đi bộ
- đổi vị trí
- quay lại vị trí cũ

Behavior lựa chọn dựa trên:
- mood
- energy
- context
- time
- personality
- cooldown
- randomness

Không gọi LLM cho từng idle animation.

Behavior scheduler nên ưu tiên:
- event-driven
- low-frequency tick
- deterministic rules ở nơi phù hợp

==================================================
10. DESKTOP MOVEMENT
==================================================

Mili có thể tự di chuyển trong desktop.

Movement phải kết hợp nhiều kiểu:

A. Random wandering
- đi ngẫu nhiên
- dừng
- idle
- đi tiếp

B. Waypoint / screen-area movement
- di chuyển giữa các vùng hợp lệ

C. Contextual movement
Ví dụ:
- Unity đang mở → Mili có thể tò mò và di chuyển gần khu vực cửa sổ
- boredom cao → wander
- user tương tác → quay lại gần user/pet home

LLM không trực tiếp điều khiển window coordinates.

Tạo abstraction tương tự:

DesktopPetController

Có thể có API:
- MoveTo(position)
- MoveRandomly()
- Wander()
- Stop()
- FaceDirection(direction)
- GoToScreenEdge()
- ReturnToHome()
- FollowCursor()
- Hide()
- Sleep()

Yêu cầu:
- không đi ra ngoài màn hình
- hỗ trợ multi-monitor nếu khả thi
- movement mượt
- easing
- không teleport nếu không cần
- cooldown
- giới hạn vùng di chuyển
- ON/OFF setting
- không di chuyển quá thường xuyên
- không gây cảm giác giật

==================================================
11. USER INTERACTION
==================================================

Mili phản ứng với:
- click
- drag
- double click
- gọi tên
- voice input
- text message
- user quay lại máy
- thay đổi context

Ví dụ:
- click → surprised/happy
- drag → playful/annoyed tùy mood
- gọi "Mili" → attentive
- user quay lại sau lâu → curious/happy
- bị bỏ mặc lâu → bored

Phản ứng phải đi qua behavior / emotion layer thay vì hard-code mọi thứ vào LLM.

==================================================
12. DESKTOP MISCHIEF / PLAYFUL ACTIONS
==================================================

Mili được phép nghịch desktop ở mức nhẹ.

Ví dụ:
- di chuyển vị trí icon desktop
- đứng cạnh một icon
- chạy theo cursor
- đi tới một cửa sổ
- hide/unhide bản thân
- các playful desktop actions khác

Nhưng desktop mischief phải là một action domain riêng.

Ví dụ flow:

Mili
 ↓
PlayfulBehavior
 ↓
DesktopPlayTool
 ↓
Permission Check
 ↓
Windows

“Di chuyển icon desktop” chỉ được thay đổi visual position.
Không được xem đó là quyền sửa/xóa file shortcut.

Nên có khả năng:
- whitelist icon / vùng được phép tác động
- blacklist system items
- restore desktop layout
- tắt hoàn toàn playful desktop actions

Mili không được tự ý nghịch những thứ ngoài phạm vi đã định nghĩa.

==================================================
13. PERMISSION SYSTEM
==================================================

Thiết kế permission thành 3 cấp.

READ:
Được phép tự động:
- đọc thời gian
- đọc trạng thái user
- đọc active application
- đọc system metadata
- đọc dữ liệu đã whitelist

INTERACT:
Được phép tự động trong whitelist:
- mở application
- focus window
- desktop pet movement
- desktop pet interaction
- cursor interaction
- playful desktop actions

DESTRUCTIVE:
LUÔN cần confirmation:
- delete file
- modify file
- rename file
- arbitrary command execution
- install/uninstall software
- kill process
- registry modification
- hành động có nguy cơ mất dữ liệu hoặc thay đổi hệ thống

Ví dụ:
User: “Xóa file này.”

Mili:
“Việc này có thể không hoàn tác được. Bạn xác nhận cho tôi xóa không?”

Permission phải được enforce bằng code/tool layer.
Không được dựa vào system prompt của LLM để đảm bảo an toàn.

LLM không được bypass permission.

==================================================
14. CONTEXT AWARENESS
==================================================

Mili có thể tự biết metadata nhẹ:
- isUserActive
- idleDuration
- activeApplication
- currentTime
- fullscreen
- currentProcessCategory

Ví dụ:
- Unity đang mở → biết user đang làm Unity
- VS Code đang mở → biết user đang code
- user idle 10 phút → biết user có thể đã rời máy

Screen capture / heavy perception:
KHÔNG được thực hiện liên tục.

Chỉ screenshot / perception khi:
- user yêu cầu
- workflow yêu cầu
- tool cụ thể được phép
- behavior thực sự cần

Không gửi screenshot vào LLM liên tục.

==================================================
15. WORK ASSISTANT
==================================================

Mili có thể hỗ trợ:
- code
- phân tích lỗi
- Unity
- xem màn hình khi được phép
- tìm thông tin
- đọc file được phép
- MCP/tools
- mở application
- hỗ trợ task
- nhắc việc
- theo dõi công việc

Mili không được tự ý thao tác hệ thống nếu chưa qua permission layer.

==================================================
16. VOICE
==================================================

Pipeline:

Microphone
↓
STT
↓
LLM
↓
Emotion / Intent
↓
Response
↓
TTS
↓
Live2D

Hiện tại:
- LLM = Ollama
- ASR = sherpa_onnx_asr
- TTS = edge_tts

Thiết kế phải cho phép thay Edge TTS bằng local TTS sau này.

Voice state nên liên kết với:
- Listening
- Thinking
- Talking
- Happy
- Sleepy
- v.v.

Nếu architecture hiện tại hỗ trợ lip sync / talk motion thì tận dụng.

==================================================
17. MEMORY
==================================================

Short-term:
- conversation context

Long-term:
- user name
- preferences
- important facts
- habits
- useful recurring information

Không lưu mọi thứ một cách mù quáng.

Memory entry nên có:
- importance
- timestamp
- category
- optional expiration
- user control

Kiến trúc phải cho phép mở rộng về sau.

==================================================
18. PERFORMANCE / RESOURCE BUDGET
==================================================

Không biến LLM thành game loop.

Không:
- gọi LLM mỗi frame
- screenshot liên tục
- perception liên tục
- TTS liên tục
- proactive không cooldown

Behavior system:
- low-frequency tick
- event-driven khi có thể
- LLM invocation chỉ khi thực sự cần
- perception chỉ khi cần
- idle loop nhẹ
- tránh allocation liên tục nếu có thể

Cần cân nhắc CPU/GPU/RAM vì đây là desktop companion chạy liên tục.

==================================================
19. SETTINGS
==================================================

Expose các setting:
- Autonomous Behavior ON/OFF
- Autonomous Movement ON/OFF
- Proactive Speaking ON/OFF
- Voice ON/OFF
- Idle Behavior Intensity
- Movement Frequency
- Movement Area
- Screen Perception ON/OFF
- Long-term Memory ON/OFF
- Tool Permissions
- Playful Desktop Actions ON/OFF

Setting chỉ là lớp UI.
Các quyền an toàn quan trọng vẫn phải enforce bằng code.

==================================================
20. LOGGING / DEBUG
==================================================

Logging rõ ràng:

[PetBrain]
[Mood]
[LifeCycle]
[Behavior]
[Emotion]
[Movement]
[Proactive]
[Context]
[Tool]
[Permission]
[Memory]

Ví dụ:

[Behavior] Selected Wander
[Permission] Allowed
[Movement] MoveTo(...)

Hoặc:

[Tool] DeleteFile
[Permission] DENIED
[Reason] Confirmation required

Nên có debug mode để xem state/mood/behavior hiện tại.

==================================================
21. SANDBOX / SAFETY TESTING
==================================================

Trước khi cho desktop tools hoạt động rộng:
- có thể test trong sandbox
- log tool request
- log permission result
- log execution result

Ví dụ:

[ToolRequest]
[PermissionCheck]
[Approved / Denied]
[Executed / Failed]

Mục tiêu là dễ kiểm tra behavior trước khi cho quyền thật.

==================================================
22. DEVELOPMENT PHASES
==================================================

Không implement tất cả cùng lúc.

PHASE 1 – Core personality / behavior
- PetBrain
- Mood
- LifeCycle
- Emotion
- Behavior Scheduler
- Idle Behavior
- Permission Core

PHASE 2 – Living behavior
- Proactive Speaking
- User Activity
- Context Awareness
- Daily Rhythm
- Mood transitions

PHASE 3 – Desktop Pet
- Autonomous Movement
- Wander
- Waypoints
- Contextual movement
- Desktop interaction
- Playful behavior

PHASE 4 – Voice
- TTS
- voice state
- lip sync nếu architecture hỗ trợ
- voice emotion

PHASE 5 – Work Assistant
- tools
- MCP
- screen perception
- application interaction
- permission workflow

PHASE 6 – Memory
- long-term memory
- preferences
- habits
- personality continuity

PHASE 7 – Polish
- settings UI
- debug panel
- behavior tuning
- performance optimization
- persistence

Sau mỗi phase:
- run project
- kiểm tra lỗi
- regression check
- giải thích file thay đổi
- giải thích behavior mới
- không tiếp tục phase tiếp theo nếu phase hiện tại chưa ổn

==================================================
23. GIT / CHANGE MANAGEMENT
==================================================

Repository này là fork riêng của tôi.

Remote dự kiến:
- origin → Votuan12345/Open-LLM-VTuber
- upstream → Open-LLM-VTuber/Open-LLM-VTuber

Mục tiêu:
- giữ main gần repo upstream
- phát triển Mili trên branch riêng, ví dụ mili-dev

Không tự ý:
- force push
- reset history
- xóa branch
- thay đổi git remote
- biến submodule thành ordinary folder
- thay đổi cấu trúc Git lớn nếu chưa được tôi đồng ý

==================================================
24. FIRST TASK – TUYỆT ĐỐI CHƯA CODE
==================================================

Đây là yêu cầu quan trọng nhất.

TRƯỚC TIÊN CHỈ ANALYZE REPOSITORY.

Không viết code.
Không tạo file mới.
Không sửa file.
Không chạy migration lớn.

Hãy:

1. Đọc cấu trúc repository.
2. Xác định module hiện tại có thể tái sử dụng.
3. Xác định chính xác Pet Mode frontend đang hoạt động thế nào.
4. Xác định proactive speaking hiện tại.
5. Xác định inner OS / thought system hiện tại.
6. Xác định Live2D emotion/action flow.
7. Xác định websocket/event flow.
8. Xác định character config architecture.
9. Xác định tool/MCP architecture.
10. Xác định memory architecture hiện tại.
11. Xác định nơi phù hợp để đặt PetBrain.
12. Xác định phần nào cần sửa backend.
13. Xác định phần nào cần sửa Electron/frontend.
14. Xác định phần nào nên giữ nguyên.
15. Xác định dependency mới nếu có.
16. Đề xuất architecture cụ thể.
17. Đưa danh sách file dự kiến thay đổi.
18. Nêu các breaking risks.
19. Nêu phương án migration/rollback.
20. Nêu các phần có thể tận dụng thay vì rewrite.
21. Ước lượng mức độ phức tạp theo từng phase.

Sau đó DỪNG LẠI.

Chờ tôi review architecture.

Chỉ sau khi tôi đồng ý mới implement Phase 1.

Không tự ý rewrite architecture hiện tại.
