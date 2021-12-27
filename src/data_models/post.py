import concurrent.futures
from itertools import repeat

import constants
from bson.objectid import ObjectId
from src.constants import SUPPORTED_CONTENT_TYPES, inline_keys, post_status
from src.utils.common import human_readable_size
from src.utils.keyboard import create_keyboard


class Post:
    """
    General class for all types of posts: Question, Answer, Comment, etc.
    """
    def __init__(self, mongodb, stackbot, chat_id=None):
        self.db = mongodb
        self.stackbot = stackbot
        self.post_type = self.__class__.__name__.lower()
        self.collection = self.db.post  # getattr(self.db, self.post_type)
        self.chat_id = chat_id
        self.supported_content_types = SUPPORTED_CONTENT_TYPES

    def get_followers(self, post_id: str):
        """
        Get all followers of the current post.
        """
        return self.collection.find_one({'_id': ObjectId(post_id)}).get('followers', [])

    def update(self, message, replied_to_post_id=None):
        """
        In ask_post state, the user can send a post in multiple messages.
        In each message, we update the current post with the message recieved.
        """
        # If content is text, we store its html version to keep styles (bold, italic, etc.)
        if message.content_type not in self.supported_content_types:
            return

        if message.content_type == 'text':
            content = {'text': message.html_text}
        else:
            # If content is a file, its file_id, mimetype, etc is saved in database for later use
            # Note that if content is a list, the last one has the highest quality
            content = getattr(message, message.content_type)
            content = vars(content[-1]) if isinstance(content, list) else vars(content)
        content['content_type'] = message.content_type

        # Save to database
        # Note: We can store metadata in the post such as data or
        # the question_id an answer belongs to
        set_data = {'date': message.date, 'post_type': self.post_type, 'replied_to_post_id': replied_to_post_id}

        output = self.collection.update_one({'chat.id': message.chat.id, 'status': post_status.PREP}, {
            '$push': {'content': content},
            '$set': set_data,
        }, upsert=True)

        _id = output.upserted_id or self.collection.find_one({
            'chat.id': message.chat.id, 'status': post_status.PREP
        })['_id']
        return _id

    def submit(self):
        """
        Save post with post_id to database.
        """
        post = self.collection.find_one({'chat.id': self.chat_id, 'status': post_status.PREP})
        if not post:
            return

        self.collection.update_one({'_id': post['_id']}, {'$set': {'status': post_status.OPEN}})
        return post['_id']

    def send_to_one(self, post_id: str, chat_id: str, preview: bool = False):
        """
        Send post with post_id to user with chat_id.

        :param post_id: Unique id of the post
        :param chat_id: Unique id of the user
        :param preview: If True, send post in preview mode. Default is False.
        """
        post_keyboard = self.get_keyboard(post_id, preview=preview)
        post_text = self.get_text(post_id)

        # Preview to user mode or send to other users
        sent_message = self.stackbot.send_message(
            chat_id=chat_id, text=post_text,
            reply_markup=post_keyboard
        )

        self.db.callback_data.insert_one({
            'post_id': post_id,
            'chat_id': chat_id,
            'message_id': sent_message.message_id,
            'preview': preview,
        })

        return sent_message

    def send_to_many(self, post_id: str, chat_ids: list):
        """
        Send post with post_id to all users.
        """
        with concurrent.futures.ThreadPoolExecutor() as executor:
            executor.map(self.send_to_one, repeat(post_id), chat_ids)

    def send_to_all(self, post_id: str):
        """
        Send post with post_id to all users.
        """
        chat_ids = map(lambda user: user['chat']['id'], self.db.users.find())
        self.send_to_many(post_id, chat_ids)

    def get_text(self, post_id: str, preview: bool = False, prettify: bool = True):
        """
        Get post text with post_id.

        :param post_id: Unique id of the post
        :return: Post text.
        """
        post = self.collection.find_one({'_id': ObjectId(post_id)})
        post_text = ""
        for content in post['content']:
            if content['content_type'] == 'text' and content['text']:
                post_text += f"{content['text']}\n"

        # Empty post text is allowed (User can send an empty post with attachments)
        if not post_text:
            post_text = constants.EMPTY_QUESTION_TEXT_MESSAGE

        # prettify message with other information such as sender, post status, etc.
        if prettify:
            post_type = self.post_type.title()
            if preview:
                post_text = constants.POST_PREVIEW_MESSAGE.format(
                    post_text=post_text, post_type=post_type
                )
            else:
                from_user = self.get_user_identity(chat_id=post['chat']['id'])
                post_text = constants.SEND_POST_TO_ALL_MESSAGE.format(
                    from_user=from_user, post_text=post_text, post_status=post['status'],
                    post_type=post_type, emoji=self.emoji,
                )

        return post_text

    def get_keyboard(self, post_id, preview=False):
        """
        Get post keyboard that has attached files + other actions on post such as like, actions menu, etc.
        """
        post = self.collection.find_one({'_id': ObjectId(post_id)})

        # get default keys
        keys, callback_data = [], []
        post_keyboard = None
        for content in post['content']:
            if content['content_type'] != 'text':
                file_name = content.get('file_name') or content['content_type']
                file_size = human_readable_size(content['file_size'])
                keys.append(f"{file_name} - {file_size}")
                callback_data.append(content['file_unique_id'])

        # add actions, like, etc. keys
        liked_by_user = self.collection.find_one({'_id': ObjectId(post_id), 'likes': self.chat_id})
        like_key = inline_keys.like if liked_by_user else inline_keys.unlike
        num_likes = len(post.get('likes', []))
        new_like_key = f'{like_key} ({num_likes})' if num_likes else f'{like_key}'

        if not preview:
            keys.extend([inline_keys.actions, new_like_key])
            callback_data.extend([inline_keys.actions, inline_keys.like])

        post_keyboard = create_keyboard(*keys, callback_data=callback_data, is_inline=True)
        return post_keyboard

    def toggle(self, post_id, key):
        exists_flag = self.collection.find_one({'_id': ObjectId(post_id), key: self.chat_id})

        if exists_flag:
            self.collection.update_one({'_id': ObjectId(post_id)}, {'$pull': {key: self.chat_id}})
        else:
            self.collection.update_one(
                {'_id': ObjectId(post_id)}, {'$addToSet': {key: self.chat_id}}
            )

    def follow(self, post_id: str):
        """
        Follow/Unfollow post with post_id.
        """
        self.toggle(post_id, 'followers')

    def like(self, post_id: str):
        """
        Like post with post_id or unlike post if already liked.
        """
        self.toggle(post_id, 'likes')

    def get_actions_keys_and_owner(self, post_id, chat_id):
        post = self.collection.find_one({'_id': post_id})
        post_owner_chat_id = post['chat']['id']

        keys = [inline_keys.back, inline_keys.comment]
        if chat_id != post_owner_chat_id:
            if chat_id in post.get('followers', []):
                keys.append(inline_keys.unfollow)
            else:
                keys.append(inline_keys.follow)

        if chat_id == post_owner_chat_id:
            current_status = post['status']
            if current_status == post_status.OPEN:
                keys.append(inline_keys.close)
            elif current_status:
                keys.append(inline_keys.open)

            keys.append(inline_keys.edit)

        return keys, post_owner_chat_id

    def open_close(self, post_id: str):
        """
        Close/Open post with post_id.
        """
        current_status = self.collection.find_one({'_id': ObjectId(post_id)})['status']
        new_status = post_status.OPEN
        if current_status == post_status.OPEN:
            new_status = post_status.CLOSED

        self.collection.update_one(
            {'_id': ObjectId(post_id)},
            {'$set': {'status': new_status}}
        )

    def get_user_identity(self, chat_id):
        """
        Return user identity.
        """
        from src.user import User
        user = User(chat_id=chat_id, mongodb=self.db, stackbot=self.stackbot)
        return user.identity
