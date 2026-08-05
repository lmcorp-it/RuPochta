"""Mail protocol utilities for IMAP and SMTP operations.

Provides context managers and helpers for safely handling IMAP and SMTP
connections with proper error handling and resource cleanup.
"""

import imaplib
import smtplib
from contextlib import contextmanager
from typing import Generator, Optional
from fastapi import HTTPException


class IMAPHelper:
    """IMAP connection helper with error handling.
    
    Provides a safe context manager for IMAP connections
    that automatically handles login/logout and errors.
    """
    
    def __init__(self, host: str, port: int = 993):
        """Initialize IMAP helper.
        
        Args:
            host: IMAP server hostname
            port: IMAP server port (default: 993 for SSL)
        """
        self.host = host
        self.port = port
    
    @contextmanager
    def connect(self, username: str, password: str) -> Generator[imaplib.IMAP4_SSL, None, None]:
        """Context manager for IMAP connection.
        
        Handles connection setup, authentication, and cleanup automatically.
        Raises HTTPException on authentication or connection errors.
        
        Args:
            username: Username for IMAP login
            password: Password for IMAP login
        
        Yields:
            Connected and authenticated IMAP4_SSL instance
        
        Raises:
            HTTPException: On authentication failure (401) or connection error (500)
        
        Example:
            >>> from utils.mail_protocol import IMAPHelper
            >>> imap_helper = IMAPHelper("mail.example.com")
            >>> with imap_helper.connect("user@example.com", "password") as imap:
            ...     imap.select("INBOX")
            ...     status, data = imap.search(None, "ALL")
        """
        imap = None
        try:
            imap = imaplib.IMAP4_SSL(self.host, self.port)
            imap.login(username, password)
            yield imap
        except imaplib.IMAP4.error as e:
            raise HTTPException(status_code=401, detail=f"IMAP login failed: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"IMAP error: {str(e)}")
        finally:
            if imap:
                try:
                    imap.logout()
                except Exception:
                    pass
    
    def select_mailbox(self, username: str, password: str, mailbox: str = "INBOX") -> imaplib.IMAP4_SSL:
        """Convenience method to connect and select mailbox.
        
        Note: Caller is responsible for closing the connection.
        
        Args:
            username: Username for IMAP login
            password: Password for IMAP login
            mailbox: Mailbox name to select (default: INBOX)
        
        Returns:
            Connected IMAP4_SSL instance with mailbox selected
        
        Raises:
            HTTPException: On authentication or selection failure
        """
        try:
            imap = imaplib.IMAP4_SSL(self.host, self.port)
            imap.login(username, password)
            imap.select(mailbox)
            return imap
        except imaplib.IMAP4.error as e:
            raise HTTPException(status_code=401, detail=f"IMAP error: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"IMAP error: {str(e)}")


class SMTPHelper:
    """SMTP connection helper with error handling.
    
    Provides a safe context manager for SMTP connections
    that automatically handles login/logout and errors.
    """
    
    def __init__(self, host: str, port: int = 465):
        """Initialize SMTP helper.
        
        Args:
            host: SMTP server hostname
            port: SMTP server port (default: 465 for SSL)
        """
        self.host = host
        self.port = port
    
    @contextmanager
    def connect(self, username: str, password: str) -> Generator[smtplib.SMTP_SSL, None, None]:
        """Context manager for SMTP connection.
        
        Handles connection setup, authentication, and cleanup automatically.
        Raises HTTPException on authentication or connection errors.
        
        Args:
            username: Username for SMTP login
            password: Password for SMTP login
        
        Yields:
            Connected and authenticated SMTP_SSL instance
        
        Raises:
            HTTPException: On authentication failure (401) or connection error (500)
        
        Example:
            >>> from utils.mail_protocol import SMTPHelper
            >>> smtp_helper = SMTPHelper("smtp.example.com")
            >>> with smtp_helper.connect("user@example.com", "password") as smtp:
            ...     smtp.sendmail(from_addr, to_addr, message)
        """
        smtp = None
        try:
            smtp = smtplib.SMTP_SSL(self.host, self.port)
            smtp.login(username, password)
            yield smtp
        except smtplib.SMTPAuthenticationError as e:
            raise HTTPException(status_code=401, detail=f"SMTP authentication failed: {str(e)}")
        except smtplib.SMTPException as e:
            raise HTTPException(status_code=500, detail=f"SMTP error: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"SMTP error: {str(e)}")
        finally:
            if smtp:
                try:
                    smtp.quit()
                except Exception:
                    pass
    
    def send_message(self, username: str, password: str, from_addr: str,
                    to_addrs: list, message: str) -> None:
        """Convenience method to connect and send email.
        
        Args:
            username: Username for SMTP login
            password: Password for SMTP login
            from_addr: Sender email address
            to_addrs: List of recipient email addresses
            message: Full email message (RFC 2822 format)
        
        Raises:
            HTTPException: On authentication or send failure
        """
        with self.connect(username, password) as smtp:
            smtp.sendmail(from_addr, to_addrs, message)
