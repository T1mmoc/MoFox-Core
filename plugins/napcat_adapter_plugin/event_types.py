from enum import Enum

class NapcatEvent(Enum):
    # napcat插件事件枚举类 
    class ON_RECEIVED(Enum): 
        """
        该分类下均为消息接受事件，只能由napcat_plugin触发
        """
        TEXT = "napcat_on_received_text"    # 接收到文本消息
        FACE = "napcat_on_received_face"    # 接收到表情消息
        REPLY = "napcat_on_received_reply"  # 接收到回复消息
        IMAGE = "napcat_on_received_image"  # 接收到图像消息
        RECORD = "napcat_on_received_record"    # 接收到语音消息
        VIDEO = "napcat_on_received_video"  # 接收到视频消息
        AT = "napcat_on_received_at"    # 接收到at消息
        DICE = "napcat_on_received_dice"    # 接收到骰子消息
        SHAKE = "napcat_on_received_shake"  # 接收到屏幕抖动消息
        JSON = "napcat_on_received_json"    # 接收到JSON消息
        RPS = "napcat_on_received_rps"  # 接收到魔法猜拳消息
        FRIEND_INPUT = "napcat_on_friend_input" # 好友正在输入
    
    class ACCOUNT(Enum):
        """
        该分类是对账户相关的操作，只能由外部触发，napcat_plugin负责处理
        """
        SET_PROFILE = "napcat_set_qq_profile"   # 设置账号信息
        GET_ONLINE_CLIENTS = "napcat_get_online_clients"    # 获取当前账号在线客户端列表
        SET_ONLINE_STATUS = "napcat_set_online_status" # 设置在线状态
        GET_FRIENDS_WITH_CATEGORY = "napcat_get_friends_with_category" # 获取好友分组列表
        SET_AVATAR = "napcat_set_qq_avatar" # 设置头像
        SEND_LIKE = "napcat_send_like"  # 点赞
        SET_FRIEND_ADD_REQUEST = "napcat_set_friend_add_request"    # 处理好友请求
        SET_SELF_LONGNICK = "napcat_set_self_longnick"  # 设置个性签名
        GET_LOGIN_INFO = "napcat_get_login_info"  # 获取登录号信息
        GET_RECENT_CONTACT = "napcat_get_recent_contact"    # 最近消息列表
        GET_STRANGER_INFO = "napcat_get_stranger_info"  # 获取(指定)账号信息
        GET_FRIEND_LIST = "napcat_get_friend_list"  # 获取好友列表
        GET_PROFILE_LIKE = "napcat_get_profile_like"    # 获取点赞列表
        DELETE_FRIEND = "napcat_delete_friend"  # 删除好友
        GET_USER_STATUS = "napcat_get_user_status"  # 获取用户状态
        GET_STATUS = "napcat_get_status"    # 获取状态
        GET_MINI_APP_ARK = "napcat_get_mini_app_ark"    # 获取小程序卡片
        SET_DIY_ONLINE_STATUS = "napcat_set_diy_online_status"  # 设置自定义在线状态
    
    class MESSAGE(Enum):
        """
        该分类是对信息相关的操作，只能由外部触发，napcat_plugin负责处理
        """
        SEND_GROUP_POKE = "napcat_send_group_poke"  # 发送群聊戳一戳
        SEND_PRIVATE_MSG = "napcat_send_private_msg"    # 发送私聊消息
        SEND_POKE = "napcat_send_friend_poke"    # 发送戳一戳
        DELETE_MSG = "napcat_delete_msg"    # 撤回消息
        GET_GROUP_MSG_HISTORY = "napcat_get_group_msg_history"  # 获取群历史消息
        GET_MSG = "napcat_get_msg"  # 获取消息详情
        GET_FORWARD_MSG = "napcat_get_forward_msg"  # 获取合并转发消息
        SET_MSG_EMOJI_LIKE = "napcat_set_msg_emoji_like"    # 贴表情
        GET_FRIEND_MSG_HISTORY = "napcat_get_friend_msg_history"    # 获取好友历史消息
        FETCH_EMOJI_LIKE = "napcat_fetch_emoji_like"    # 获取贴表情详情
        SEND_FORWARF_MSG = "napcat_send_forward_msg"    # 发送合并转发消息
        GET_RECOED = "napcat_get_record"    # 获取语音消息详情
        SEND_GROUP_AI_RECORD = "napcat_send_group_ai_record"    # 发送群AI语音

    class GROUP(Enum):
        """
        该分类是对群聊相关的操作，只能由外部触发，napcat_plugin负责处理
        """



